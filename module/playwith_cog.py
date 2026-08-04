"""Persistent per-game party panels."""

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


def _game_key(game: str) -> str:
    return hashlib.sha256(game.encode("utf-8")).hexdigest()[:16]


def _bounded(value: str, limit: int = EMBED_FIELD_VALUE_LIMIT) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


class PartyPanelButton(discord.ui.Button):
    def __init__(self, cog: "PlayWithCog", game: str, role: Optional[str], index: int):
        kind = "toggle" if role is None else "role"
        label = "모집 / 역할 미정" if role is None else role[:80]
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.success
                if role is None
                else discord.ButtonStyle.primary
            ),
            custom_id=f"party:{kind}:{_game_key(game)}:{index}",
        )
        self.cog = cog
        self.game = game
        self.role = role

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_panel_click(interaction, self.game, self.role)


class PartyPanelView(discord.ui.View):
    """One persistent view per game; IDs never contain the raw game name."""

    def __init__(self, cog: "PlayWithCog", game: str):
        super().__init__(timeout=None)
        roles = GAMES[game]["roles"]
        if len(roles) + 1 > MAX_COMPONENTS:
            return
        if not roles:
            self.add_item(PartyPanelButton(cog, game, None, 0))
            return
        self.add_item(PartyPanelButton(cog, game, None, 0))
        for index, role in enumerate(roles, start=1):
            self.add_item(PartyPanelButton(cog, game, role, index))


class PlayWithCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        repository: Optional[PartyRepository] = None,
        settings: Optional[GuildSettingsRepository] = None,
    ):
        self.bot = bot
        self.db = repository or create_party_repository()
        self.settings = settings or create_guild_settings_repository()
        self.views = {game: PartyPanelView(self, game) for game in GAMES}
        self._panels_restored = False
        if bot is not None:
            for view in self.views.values():
                if view.children:
                    bot.add_view(view)
        self.cleanup_parties.start()

    def cog_unload(self):
        self.cleanup_parties.cancel()

    async def get_active_party(self, guild_id: int, game: str):
        return await run_db(self.db.get_party, guild_id, game)

    async def create_party(self, guild_id: int, game: str):
        return await run_db(self.db.create_party, guild_id, game, int(time.time()))

    async def delete_party(self, guild_id: int, game: str):
        await run_db(self.db.delete_party, guild_id, game)

    async def get_participants(self, guild_id: int, game: str):
        return await run_db(self.db.get_participants, guild_id, game)

    async def add_participant(
        self, guild_id: int, game: str, user_id: int, role: Optional[str] = None
    ):
        return await run_db(
            self.db.add_participant,
            guild_id,
            game,
            user_id,
            role,
            GAMES[game]["max_players"],
        )

    async def remove_participant(self, guild_id: int, game: str, user_id: int):
        await run_db(self.db.remove_participant, guild_id, game, user_id)

    async def get_user_party(self, guild_id: int, user_id: int):
        return await run_db(self.db.get_user_party, guild_id, user_id)

    async def _reject_invalid_interaction(
        self, interaction: discord.Interaction, game: str
    ) -> bool:
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

        panels = await run_db(self.settings.get_party_panels, guild_id)
        message = getattr(interaction, "message", None)
        if message is None or panels.get(game) != message.id:
            await interaction.response.send_message(
                "❌ 현재 서버의 최신 파티 패널이 아닙니다.", ephemeral=True
            )
            return True
        return False

    async def handle_panel_click(
        self, interaction: discord.Interaction, game: str, role: Optional[str]
    ) -> None:
        if await self._reject_invalid_interaction(interaction, game):
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with panel_lock(guild_id, f"party:{game}"):
            current_game = await self.get_user_party(guild_id, user_id)
            party = await self.get_active_party(guild_id, game)
            participants = await self.get_participants(guild_id, game) if party else {}

            if current_game and current_game != game:
                result = f"⚠️ 이미 `{current_game}` 파티에 참가 중입니다."
            elif current_game == game and participants.get(user_id) == role:
                await self.remove_participant(guild_id, game, user_id)
                participants = await self.get_participants(guild_id, game)
                if not participants:
                    await self.delete_party(guild_id, game)
                result = f"👋 `{game}` 파티에서 나갔습니다."
            else:
                created = False
                if party is None:
                    created = await self.create_party(guild_id, game)
                if await self.add_participant(guild_id, game, user_id, role):
                    result = (
                        f"✅ `{game}` 모집을 시작했습니다. 방장은 {interaction.user.mention}입니다."
                        if created
                        else f"✅ `{game}` 파티 역할을 `{role or '미정'}`(으)로 정했습니다."
                    )
                else:
                    if created:
                        await self.delete_party(guild_id, game)
                    result = "⚠️ 파티가 가득 찼거나 선택한 역할이 이미 찼습니다."

            await self.render_game_panel(guild_id, game)
        await interaction.followup.send(result, ephemeral=True)

    async def _current_panel(self, guild: discord.Guild, game: str):
        info = GAMES[game]
        if len(info["roles"]) + 1 > MAX_COMPONENTS:
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
                self.views[game],
            )

        created_at = int(party[0])
        host_id = next(iter(participants), None)
        host = guild.get_member(host_id) if host_id is not None else None
        host_text = host.mention if host else (f"<@{host_id}>" if host_id else "없음")
        embed = discord.Embed(
            title=f"🎮 {game} 파티 모집",
            description=(
                f"방장: {host_text}\n"
                f"현재 인원: {len(participants)} / {info['max_players']}\n"
                f"만료: <t:{created_at + PARTY_LIFETIME_SECONDS}:R>"
            ),
            color=discord.Color.green(),
        )
        if info["roles"]:
            role_members = {assigned: uid for uid, assigned in participants.items()}
            lines = [
                f"{role}: <@{role_members[role]}>" if role in role_members else f"{role}: 비어 있음"
                for role in info["roles"]
            ]
            unassigned = [f"<@{uid}>" for uid, assigned in participants.items() if assigned is None]
            if unassigned:
                lines.append("역할 미정: " + ", ".join(unassigned))
            embed.add_field(name="역할별 자리", value=_bounded("\n".join(lines)), inline=False)
        return embed, self.views[game]

    async def render_game_panel(self, guild_id: int, game: str) -> None:
        if game not in GAMES or self.bot is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel_id = await run_db(self.settings.get_party_channel, guild_id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return

        panels = await run_db(self.settings.get_party_panels, guild_id)
        embed, view = await self._current_panel(guild, game)
        try:
            message = await upsert_panel(
                channel, panels.get(game), embed=embed, view=view
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("파티 패널을 갱신하지 못했습니다: guild=%s game=%s", guild_id, game)
            return
        if panels.get(game) != message.id:
            await run_db(self.settings.set_party_panel, guild_id, game, message.id)

    async def ensure_panels(self, guild: discord.Guild) -> None:
        channel_id = await run_db(self.settings.get_party_channel, guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return

        stored = await run_db(self.settings.get_party_panels, guild.id)
        for game, message_id in stored.items():
            if game in GAMES:
                continue
            try:
                message = await channel.fetch_message(message_id)
                bot_user = getattr(self.bot, "user", None)
                if bot_user is not None and message.author.id == bot_user.id:
                    await message.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("오래된 파티 패널을 정리하지 못했습니다: guild=%s game=%s", guild.id, game)
                continue
            await run_db(self.settings.delete_party_panel, guild.id, game)

        for game in GAMES:
            async with panel_lock(guild.id, f"party:{game}"):
                await self.render_game_panel(guild.id, game)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._panels_restored or self.bot is None:
            return
        self._panels_restored = True
        for guild in self.bot.guilds:
            await self.ensure_panels(guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild_id = member.guild.id
        game = await self.get_user_party(guild_id, member.id)
        if not game:
            return
        async with panel_lock(guild_id, f"party:{game}"):
            await self.remove_participant(guild_id, game, member.id)
            if not await self.get_participants(guild_id, game):
                await self.delete_party(guild_id, game)
            await self.render_game_panel(guild_id, game)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await run_db(self.db.delete_guild, guild.id)

    @tasks.loop(minutes=10)
    async def cleanup_parties(self):
        cutoff = int(time.time()) - PARTY_LIFETIME_SECONDS
        expired = await run_db(self.db.delete_expired_parties, cutoff)
        for guild_id, game in expired:
            async with panel_lock(guild_id, f"party:{game}"):
                await self.render_game_panel(guild_id, game)
        if expired:
            print(f"Deleted expired parties: {expired}")


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayWithCog(bot))
