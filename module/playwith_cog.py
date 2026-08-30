"""Persistent per-game party panels."""

import asyncio
import hashlib
import logging
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

from module.config import GAMES
from module.database import (
    GuildSettingsRepository,
    PartyRepository,
    create_guild_settings_repository,
    create_party_repository,
    run_db,
)
from module.panel import panel_lock, upsert_panel

logger = logging.getLogger(__name__)
PARTY_LIFETIME_SECONDS = 24 * 60 * 60
MAX_COMPONENTS = 25
EMBED_FIELD_VALUE_LIMIT = 1_024
SELECTOR_PANEL_KEY = "__game_selector__"


def _game_component_key(game_name: str) -> str:
    return hashlib.sha256(game_name.encode("utf-8")).hexdigest()[:16]


def _role_component_key(role_name: str) -> str:
    return hashlib.sha256(role_name.encode("utf-8")).hexdigest()[:16]


def _truncate_embed_field(
    text: str, max_length: int = EMBED_FIELD_VALUE_LIMIT
) -> str:
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


class PartyPanelButton(discord.ui.Button):
    def __init__(
        self,
        cog: "PlayWithCog",
        game: str,
        action: str,
        role: Optional[str] = None,
        *,
        disabled: bool = False,
    ):
        labels = {"join": "참가", "leave": "나가기"}
        suffix = _role_component_key(role) if role is not None else action
        super().__init__(
            label=role[:80] if role is not None else labels[action],
            style=(
                discord.ButtonStyle.danger
                if action == "leave"
                else discord.ButtonStyle.primary
            ),
            custom_id=(
                f"party:{action}:{_game_component_key(game)}:{suffix}"
            ),
            disabled=disabled,
        )
        self.cog = cog
        self.game = game
        self.action = action
        self.role = role

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_panel_click(
            interaction, self.game, self.action, self.role
        )


class PartyPanelView(discord.ui.View):
    """One persistent view per game; IDs never contain the raw game name."""

    def __init__(self, cog: "PlayWithCog", game: str):
        super().__init__(timeout=None)
        roles = GAMES[game]["roles"]
        if len(roles) + 1 > MAX_COMPONENTS:
            return
        if roles:
            for role in roles:
                self.add_item(PartyPanelButton(cog, game, "role", role))
        else:
            self.add_item(PartyPanelButton(cog, game, "join"))
        self.add_item(PartyPanelButton(cog, game, "leave"))


class GameSelectorButton(discord.ui.Button):
    def __init__(self, cog: "PlayWithCog", game: str):
        super().__init__(
            label=game[:80],
            style=discord.ButtonStyle.success,
            custom_id=f"party:select:{_game_component_key(game)}",
        )
        self.cog = cog
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_game_select(interaction, self.game)


class GameSelectorView(discord.ui.View):
    def __init__(self, cog: "PlayWithCog"):
        super().__init__(timeout=None)
        for game in list(GAMES)[:MAX_COMPONENTS]:
            self.add_item(GameSelectorButton(cog, game))


class PlayWithCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        party_repository: Optional[PartyRepository] = None,
        settings_repository: Optional[GuildSettingsRepository] = None,
    ):
        self.bot = bot
        self.party_repository = party_repository or create_party_repository()
        self.settings_repository = (
            settings_repository or create_guild_settings_repository()
        )
        self.party_views = {game: PartyPanelView(self, game) for game in GAMES}
        self.selector_view = GameSelectorView(self)
        self._panels_restored = False
        self._restore_lock = asyncio.Lock()
        if bot is not None:
            if self.selector_view.children:
                bot.add_view(self.selector_view)
            for view in self.party_views.values():
                if view.children:
                    bot.add_view(view)
        self.cleanup_parties.start()

    def cog_unload(self):
        self.cleanup_parties.cancel()

    async def get_active_party(self, guild_id: int, game: str):
        return await run_db(self.party_repository.get_party, guild_id, game)

    async def create_party(self, guild_id: int, game: str, host_id: Optional[int] = None):
        return await run_db(
            self.party_repository.create_party,
            guild_id,
            game,
            int(time.time()),
            host_id,
        )

    async def get_party_host(self, guild_id: int, game: str):
        return await run_db(
            self.party_repository.get_party_host, guild_id, game
        )

    async def delete_party(self, guild_id: int, game: str):
        await run_db(self.party_repository.delete_party, guild_id, game)

    async def get_participants(self, guild_id: int, game: str):
        return await run_db(
            self.party_repository.get_participants, guild_id, game
        )

    async def add_participant(
        self, guild_id: int, game: str, user_id: int, role: Optional[str] = None
    ):
        return await run_db(
            self.party_repository.add_participant,
            guild_id,
            game,
            user_id,
            role,
            GAMES[game]["max_players"],
        )

    async def remove_participant(self, guild_id: int, game: str, user_id: int):
        return await run_db(
            self.party_repository.remove_participant, guild_id, game, user_id
        )

    async def get_user_party(self, guild_id: int, user_id: int):
        return await run_db(
            self.party_repository.get_user_party, guild_id, user_id
        )

    async def _reject_invalid_interaction(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        guild_id = interaction.guild_id
        if guild is None or guild_id is None:
            await interaction.response.send_message(
                "❌ 이 버튼은 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return True
        if guild.id != guild_id or guild.get_member(interaction.user.id) is None:
            await interaction.response.send_message(
                "❌ 현재 서버 멤버만 이 패널을 사용할 수 있습니다.", ephemeral=True
            )
            return True

        return False

    async def _is_current_panel(self, interaction: discord.Interaction, game: str) -> bool:
        party_channel_id, panels = await asyncio.gather(
            run_db(
                self.settings_repository.get_party_channel,
                interaction.guild_id,
            ),
            run_db(
                self.settings_repository.get_party_panels,
                interaction.guild_id,
            ),
        )
        message = getattr(interaction, "message", None)
        interaction_channel_id = getattr(interaction, "channel_id", None)
        if interaction_channel_id is None and message is not None:
            interaction_channel_id = getattr(
                getattr(message, "channel", None), "id", None
            )
        return (
            message is not None
            and party_channel_id is not None
            and interaction_channel_id == party_channel_id
            and panels.get(game) == message.id
        )

    async def _is_current_selector(self, interaction: discord.Interaction) -> bool:
        party_channel_id, panels = await asyncio.gather(
            run_db(
                self.settings_repository.get_party_channel,
                interaction.guild_id,
            ),
            run_db(
                self.settings_repository.get_party_panels,
                interaction.guild_id,
            ),
        )
        message = getattr(interaction, "message", None)
        interaction_channel_id = getattr(interaction, "channel_id", None)
        if interaction_channel_id is None and message is not None:
            interaction_channel_id = getattr(
                getattr(message, "channel", None), "id", None
            )
        return (
            message is not None
            and party_channel_id is not None
            and interaction_channel_id == party_channel_id
            and panels.get(SELECTOR_PANEL_KEY) == message.id
        )

    async def handle_game_select(
        self, interaction: discord.Interaction, game: str
    ) -> None:
        if await self._reject_invalid_interaction(interaction):
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with panel_lock(guild_id, f"party:{game}"):
            if not await self._is_current_selector(interaction):
                await interaction.followup.send(
                    "❌ 현재 서버의 최신 게임 선택 패널이 아닙니다.", ephemeral=True
                )
                return

            current_game = await self.get_user_party(guild_id, user_id)
            party = await self.get_active_party(guild_id, game)
            if current_game:
                response_message = f"⚠️ 이미 `{current_game}` 파티에 참가 중입니다."
            elif party is not None:
                response_message = f"ℹ️ `{game}` 파티가 이미 모집 중입니다. 편성 패널을 확인해 주세요."
            elif await self.create_party(guild_id, game, user_id):
                response_message = (
                    f"✅ `{game}` 모집을 시작했습니다. "
                    f"방장은 {interaction.user.mention}입니다."
                )
            else:
                current_game = await self.get_user_party(guild_id, user_id)
                response_message = (
                    f"⚠️ 이미 `{current_game}` 파티에 참가 중입니다."
                    if current_game
                    else "⚠️ 다른 사용자가 먼저 모집을 시작했습니다."
                )

            if await self.get_active_party(guild_id, game) is not None:
                await self.render_game_panel(guild_id, game)
        await interaction.followup.send(response_message, ephemeral=True)

    async def handle_panel_click(
        self,
        interaction: discord.Interaction,
        game: str,
        action: str,
        role: Optional[str] = None,
    ) -> None:
        if await self._reject_invalid_interaction(interaction):
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with panel_lock(guild_id, f"party:{game}"):
            if not await self._is_current_panel(interaction, game):
                await interaction.followup.send(
                    "❌ 현재 서버의 최신 파티 패널이 아닙니다.", ephemeral=True
                )
                return

            party = await self.get_active_party(guild_id, game)
            participants = await self.get_participants(guild_id, game) if party else {}
            current_game = await self.get_user_party(guild_id, user_id)

            if party is None:
                response_message = "⚠️ 이 파티는 이미 종료되었습니다. 게임 선택 패널을 이용해 주세요."
            elif action == "leave":
                if current_game != game:
                    response_message = f"ℹ️ `{game}` 파티에 참가하고 있지 않습니다."
                else:
                    await self.remove_participant(guild_id, game, user_id)
                    response_message = f"👋 `{game}` 파티에서 나갔습니다."
            elif current_game and current_game != game:
                response_message = f"⚠️ 이미 `{current_game}` 파티에 참가 중입니다."
            elif current_game == game and participants.get(user_id) == role:
                response_message = f"ℹ️ 이미 `{role or '참가'}` 상태입니다. 나가려면 `나가기` 버튼을 눌러 주세요."
            else:
                if await self.add_participant(guild_id, game, user_id, role):
                    response_message = f"✅ `{game}` 파티 역할을 `{role or '참가'}`(으)로 정했습니다."
                else:
                    current_game = await self.get_user_party(guild_id, user_id)
                    response_message = (
                        f"⚠️ 이미 `{current_game}` 파티에 참가 중입니다."
                        if current_game and current_game != game
                        else "⚠️ 파티가 가득 찼거나 선택한 역할이 이미 찼습니다."
                    )

            await self.render_game_panel(guild_id, game)
        await interaction.followup.send(response_message, ephemeral=True)

    async def _build_game_panel(self, guild: discord.Guild, game: str):
        game_config = GAMES[game]
        if len(game_config["roles"]) + 1 > MAX_COMPONENTS:
            return (
                discord.Embed(
                    title=f"⚠️ {game} 파티 패널 비활성",
                    description="역할 수가 Discord 버튼 25개 한도를 넘습니다. games.json을 수정하고 재시작하세요.",
                    color=discord.Color.red(),
                ),
                None,
            )

        party = await self.get_active_party(guild.id, game)
        participants = await self.get_participants(guild.id, game) if party else {}
        if not party:
            return (
                discord.Embed(
                    title=f"🎮 {game}",
                    description="현재 모집이 없습니다. 아래 버튼을 눌러 모집을 시작하세요.",
                    color=discord.Color.dark_grey(),
                ),
                PartyPanelView(self, game),
            )

        created_at = int(party[0])
        host_id = await self.get_party_host(guild.id, game)
        host = guild.get_member(host_id) if host_id is not None else None
        host_text = host.mention if host else (f"<@{host_id}>" if host_id else "없음")
        embed = discord.Embed(
            title=f"🎮 {game} 파티 모집",
            description=(
                f"방장: {host_text}\n"
                f"현재 인원: {len(participants)} / {game_config['max_players']}\n"
                f"만료: <t:{created_at + PARTY_LIFETIME_SECONDS}:R>"
            ),
            color=discord.Color.green(),
        )
        if game_config["roles"]:
            role_members = {assigned: uid for uid, assigned in participants.items()}
            lines = [
                f"{role}: <@{role_members[role]}>" if role in role_members else f"{role}: 비어 있음"
                for role in game_config["roles"]
            ]
            unassigned = [f"<@{uid}>" for uid, assigned in participants.items() if assigned is None]
            if unassigned:
                lines.append("역할 미정: " + ", ".join(unassigned))
            embed.add_field(
                name="역할별 자리",
                value=_truncate_embed_field("\n".join(lines)),
                inline=False,
            )
        return embed, PartyPanelView(self, game)

    async def _delete_stored_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        panel_key: str,
        message_id: int,
    ) -> bool:
        current_panel_ids = await run_db(
            self.settings_repository.get_party_panels, guild.id
        )
        if current_panel_ids.get(panel_key) != message_id:
            return True
        try:
            message = await channel.fetch_message(message_id)
            bot_user = getattr(self.bot, "user", None)
            if bot_user is not None and message.author.id == bot_user.id:
                await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "파티 패널을 정리하지 못했습니다: guild=%s key=%s",
                guild.id,
                panel_key,
            )
            return False
        current_panel_ids = await run_db(
            self.settings_repository.get_party_panels, guild.id
        )
        if current_panel_ids.get(panel_key) == message_id:
            await run_db(
                self.settings_repository.delete_party_panel,
                guild.id,
                panel_key,
            )
        return True

    async def render_game_selector(self, guild: discord.Guild) -> None:
        channel_id = await run_db(
            self.settings_repository.get_party_channel, guild.id
        )
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        panels = await run_db(
            self.settings_repository.get_party_panels, guild.id
        )
        embed = discord.Embed(
            title="🎮 게임 선택",
            description=(
                "아래 버튼에서 모집할 게임을 선택하세요."
                if self.selector_view.children
                else "설정된 게임이 없습니다. games.json을 확인해 주세요."
            ),
            color=discord.Color.blurple(),
        )
        try:
            message = await upsert_panel(
                channel,
                panels.get(SELECTOR_PANEL_KEY),
                embed=embed,
                view=self.selector_view if self.selector_view.children else None,
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("게임 선택 패널을 갱신하지 못했습니다: guild=%s", guild.id)
            return
        if panels.get(SELECTOR_PANEL_KEY) != message.id:
            await run_db(
                self.settings_repository.set_party_panel,
                guild.id,
                SELECTOR_PANEL_KEY,
                message.id,
            )

    async def render_game_panel(self, guild_id: int, game: str) -> None:
        if game not in GAMES or self.bot is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel_id = await run_db(
            self.settings_repository.get_party_channel, guild_id
        )
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return

        panels = await run_db(
            self.settings_repository.get_party_panels, guild_id
        )
        party = await self.get_active_party(guild_id, game)
        if party is None:
            message_id = panels.get(game)
            if message_id is not None:
                await self._delete_stored_panel(guild, channel, game, message_id)
            return
        embed, view = await self._build_game_panel(guild, game)
        try:
            message = await upsert_panel(
                channel, panels.get(game), embed=embed, view=view
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("파티 패널을 갱신하지 못했습니다: guild=%s game=%s", guild_id, game)
            return
        if panels.get(game) != message.id:
            await run_db(
                self.settings_repository.set_party_panel,
                guild_id,
                game,
                message.id,
            )

    async def ensure_panels(self, guild: discord.Guild) -> None:
        channel_id = await run_db(
            self.settings_repository.get_party_channel, guild.id
        )
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return

        stored_panel_ids = await run_db(
            self.settings_repository.get_party_panels, guild.id
        )
        for game, message_id in stored_panel_ids.items():
            if game == SELECTOR_PANEL_KEY:
                continue
            if game in GAMES and await self.get_active_party(guild.id, game) is not None:
                continue
            async with panel_lock(guild.id, f"party:{game}"):
                await self._delete_stored_panel(guild, channel, game, message_id)

        async with panel_lock(guild.id, "party:selector"):
            await self.render_game_selector(guild)
        for game in GAMES:
            if await self.get_active_party(guild.id, game) is not None:
                async with panel_lock(guild.id, f"party:{game}"):
                    await self.render_game_panel(guild.id, game)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._panels_restored or self.bot is None:
            return
        async with self._restore_lock:
            if self._panels_restored:
                return
            restoration_failed = False
            for guild in self.bot.guilds:
                try:
                    await self.ensure_panels(guild)
                except Exception:
                    restoration_failed = True
                    logger.exception("파티 패널 startup 복구 실패: guild=%s", guild.id)
            self._panels_restored = not restoration_failed

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild_id = member.guild.id
        game = await self.get_user_party(guild_id, member.id)
        if not game:
            return
        async with panel_lock(guild_id, f"party:{game}"):
            await self.remove_participant(guild_id, game, member.id)
            await self.render_game_panel(guild_id, game)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await run_db(self.party_repository.delete_guild, guild.id)

    @tasks.loop(minutes=10)
    async def cleanup_parties(self):
        expiration_cutoff = int(time.time()) - PARTY_LIFETIME_SECONDS
        expiration_candidates = await run_db(
            self.party_repository.list_expired_parties, expiration_cutoff
        )
        expired_parties = []
        for guild_id, game in expiration_candidates:
            async with panel_lock(guild_id, f"party:{game}"):
                deleted = await run_db(
                    self.party_repository.delete_party_if_expired,
                    guild_id,
                    game,
                    expiration_cutoff,
                )
                if deleted:
                    expired_parties.append((guild_id, game))
                    await self.render_game_panel(guild_id, game)
        if expired_parties:
            print(f"Deleted expired parties: {expired_parties}")


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayWithCog(bot))
