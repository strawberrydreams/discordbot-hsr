"""서버별 봇 설정.

봇이 여러 서버에 설치되므로 채널 ID를 환경변수에 둘 수 없다. 설정 시작과
현재 상태와 웹 공지 opt-in은 Discord에서, 채널과 필터 값은 호스트의 로컬 웹
관리에서 다룬다.
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)
from module.panel import (
    drop_panel_locks,
    is_sendable_panel_channel,
    panel_lock,
)
from module.party_cog import PartyCog

logger = logging.getLogger(__name__)


class SetupView(discord.ui.View):
    def __init__(self, cog: "GuildSettingsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="봇 채널 만들기",
        style=discord.ButtonStyle.primary,
        custom_id="setup:start",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "❌ 서버 안에서만 설정할 수 있습니다.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ 서버 관리 권한이 필요합니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        party_channel = await self.cog._ensure_bot_channels(guild)
        await interaction.followup.send(
            f"✅ 봇 채널을 준비했습니다: {party_channel.mention}",
            ephemeral=True,
        )


class GuildSettingsCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        settings_repository: Optional[GuildSettingsRepository] = None,
    ):
        self.bot = bot
        self.settings_repository = (
            settings_repository or create_guild_settings_repository()
        )
        self.setup_view = SetupView(self)
        if bot is not None:
            bot.add_view(self.setup_view)

    설정 = app_commands.Group(
        name="설정",
        description="이 서버에서 봇이 사용할 채널을 지정합니다. (Administrator 필요)",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is not None and permissions.administrator:
            return True
        raise app_commands.MissingPermissions(["administrator"])

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """봇이 서버에서 제거되면 그 서버의 설정을 남기지 않는다."""
        await run_db(self.settings_repository.delete_guild, guild.id)
        drop_panel_locks(guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await run_db(
            self.settings_repository.clear_channel,
            channel.guild.id,
            channel.id,
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        channel = guild.system_channel
        if channel and channel.permissions_for(guild.me).send_messages:
            await channel.send(
                "안녕하세요! 아래 버튼으로 봇 전용 채널을 만들 수 있습니다.",
                view=self.setup_view,
            )

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in getattr(self.bot, "guilds", ()):
            try:
                await self._rename_legacy_party_channel(guild)
            except Exception:
                logger.exception(
                    "기존 파티 채널 이름 확인 실패: guild=%s", guild.id
                )

    async def _rename_legacy_party_channel(self, guild: discord.Guild):
        party_channel_id = await run_db(
            self.settings_repository.get_party_channel, guild.id
        )
        party_channel = (
            guild.get_channel(party_channel_id) if party_channel_id else None
        )
        if party_channel is None or party_channel.name != "🎮-파티":
            return party_channel
        try:
            await party_channel.edit(name="🎮-디스코-파티")
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "기존 파티 채널 이름을 바꾸지 못했습니다: guild=%s",
                guild.id,
            )
        return party_channel

    async def _ensure_bot_channels(self, guild: discord.Guild) -> discord.TextChannel:
        async with panel_lock(guild.id, "setup"):
            channel = await self.ensure_bot_channels(guild)
            await self._ensure_panels(guild)
            return channel

    async def _ensure_panels(self, guild: discord.Guild) -> None:
        get_cog = getattr(self.bot, "get_cog", None)
        party_cog = get_cog(PartyCog.__name__) if get_cog else None
        if party_cog is not None:
            await party_cog.ensure_panels(guild)

    async def ensure_bot_channels(self, guild: discord.Guild) -> discord.TextChannel:
        party_channel = await self._rename_legacy_party_channel(guild)
        if party_channel:
            return party_channel

        category = discord.utils.get(guild.categories, name="🤖 봇")
        if category is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    embed_links=True,
                    attach_files=True,
                ),
            }
            category = await guild.create_category("🤖 봇", overwrites=overwrites)

        party_channel = await guild.create_text_channel(
            "🎮-디스코-파티", category=category
        )
        await run_db(
            self.settings_repository.set_party_channel,
            guild.id,
            party_channel.id,
        )
        return party_channel

    @설정.command(name="시작", description="봇 전용 파티 채널을 만듭니다.")
    async def _start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        party_channel = await self._ensure_bot_channels(interaction.guild)
        await interaction.followup.send(
            f"✅ 봇 채널을 준비했습니다: {party_channel.mention}",
            ephemeral=True,
        )

    @설정.command(name="공지허용", description="웹 관리 공지를 허용하거나 차단합니다.")
    @app_commands.describe(허용="설정한 공지 채널로 웹 관리 공지를 받을지 선택합니다.")
    async def _set_host_announcements(
        self, interaction: discord.Interaction, 허용: bool
    ):
        await run_db(
            self.settings_repository.set_allow_host_announce,
            interaction.guild_id,
            허용,
        )
        await interaction.response.send_message(
            f"✅ 웹 관리 공지를 {'허용' if 허용 else '차단'}했습니다.",
            ephemeral=True,
        )

    @설정.command(name="확인", description="현재 서버의 설정을 봅니다.")
    async def _show_settings(self, interaction: discord.Interaction):
        party_channel_id = await run_db(
            self.settings_repository.get_party_channel, interaction.guild_id
        )
        announcement_channel_id = await run_db(
            self.settings_repository.get_announcement_channel,
            interaction.guild_id,
        )
        event_channel_id = await run_db(
            self.settings_repository.get_event_channel,
            interaction.guild_id,
        )
        allow_host_announce = await run_db(
            self.settings_repository.get_allow_host_announce,
            interaction.guild_id,
        )
        forbidden_filter_enabled = await run_db(
            self.settings_repository.get_forbidden_filter_enabled,
            interaction.guild_id,
        )
        party_value = "미지정 — 웹 관리에서 지정"
        if party_channel_id:
            party_value = f"<#{party_channel_id}>"
            guild = getattr(interaction, "guild", None)
            channel = (
                guild.get_channel(party_channel_id)
                if guild is not None
                else None
            )
            if guild is not None and not is_sendable_panel_channel(guild, channel):
                party_value += " — 삭제됨 또는 권한 부족"

        embed = discord.Embed(title="⚙️ 이 서버의 봇 설정", color=discord.Color.blurple())
        embed.add_field(
            name="파티 패널 채널",
            value=party_value,
            inline=False,
        )
        embed.add_field(
            name="웹 공지 채널",
            value=(
                f"<#{announcement_channel_id}>"
                if announcement_channel_id
                else "미지정 — 웹 관리에서 지정"
            ),
            inline=False,
        )
        embed.add_field(
            name="이벤트 전용 채널",
            value=(
                f"<#{event_channel_id}>"
                if event_channel_id
                else "미지정 — 모든 채널에서 사용 가능"
            ),
            inline=False,
        )
        embed.add_field(
            name="웹 관리 공지",
            value="허용" if allow_host_announce else "차단",
            inline=False,
        )
        embed.add_field(
            name="금지어 필터",
            value="켜짐" if forbidden_filter_enabled else "꺼짐",
            inline=False,
        )
        embed.set_footer(
            text="웹 공지 허용은 /설정 공지허용, 채널·금지어 설정은 localhost 웹 관리에서 변경합니다."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildSettingsCog(bot))
