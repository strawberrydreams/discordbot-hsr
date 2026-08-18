"""서버별 봇 설정.

봇이 여러 서버에 설치되므로 채널 ID를 환경변수에 둘 수 없다(운영자는 남의
서버 채널 ID를 모른다). 각 서버 관리자가 이 명령으로 직접 지정한다.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)
from module.panel import drop_panel_locks, panel_lock


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
        party = await self.cog._ensure_bot_channels(guild)
        await interaction.followup.send(
            f"✅ 봇 채널을 준비했습니다: {party.mention}", ephemeral=True
        )


class GuildSettingsCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, settings: Optional[GuildSettingsRepository] = None
    ):
        self.bot = bot
        self.settings = settings or create_guild_settings_repository()
        self.setup_view = SetupView(self)
        if bot is not None:
            bot.add_view(self.setup_view)

    설정 = app_commands.Group(
        name="설정",
        description="이 서버에서 봇이 사용할 채널을 지정합니다. (서버 관리 권한 필요)",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """봇이 서버에서 제거되면 그 서버의 설정을 남기지 않는다."""
        await run_db(self.settings.delete_guild, guild.id)
        drop_panel_locks(guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await run_db(self.settings.clear_channel, channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        channel = guild.system_channel
        if channel and channel.permissions_for(guild.me).send_messages:
            await channel.send(
                "안녕하세요! 아래 버튼으로 봇 전용 채널을 만들 수 있습니다.",
                view=self.setup_view,
            )

    async def _ensure_bot_channels(self, guild: discord.Guild) -> discord.TextChannel:
        async with panel_lock(guild.id, "setup"):
            channel = await self.ensure_bot_channels(guild)
            await self._ensure_panels(guild)
            return channel

    async def _ensure_panels(self, guild: discord.Guild) -> None:
        get_cog = getattr(self.bot, "get_cog", None)
        play_cog = get_cog("PlayWithCog") if get_cog else None
        if play_cog is not None:
            await play_cog.ensure_panels(guild)

    async def ensure_bot_channels(self, guild: discord.Guild) -> discord.TextChannel:
        party_id = await run_db(self.settings.get_party_channel, guild.id)
        party = guild.get_channel(party_id) if party_id else None
        if party:
            return party

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

        party = await guild.create_text_channel("🎮-파티", category=category)
        await run_db(self.settings.set_party_channel, guild.id, party.id)
        return party

    @설정.command(name="시작", description="봇 전용 파티 채널을 만듭니다.")
    async def _start(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        party = await self._ensure_bot_channels(inter.guild)
        await inter.followup.send(
            f"✅ 봇 채널을 준비했습니다: {party.mention}", ephemeral=True
        )

    @설정.command(name="파티채널", description="파티 패널을 표시할 채널을 지정합니다.")
    @app_commands.describe(채널="비워두면 현재 채널로 지정됩니다.")
    async def _party_channel(
        self, inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None
    ):
        target = 채널 or inter.channel
        if not isinstance(target, discord.TextChannel):
            await inter.response.send_message(
                "❌ 텍스트 채널에서 실행하거나 텍스트 채널을 지정해 주세요.",
                ephemeral=True,
            )
            return
        await inter.response.defer(ephemeral=True)
        await run_db(self.settings.set_party_channel, inter.guild_id, target.id)
        await self._ensure_panels(inter.guild)
        await inter.followup.send(
            f"✅ 파티 패널 채널을 {target.mention} 으로 지정했습니다.", ephemeral=True
        )

    @설정.command(name="공지허용", description="파티 호스트 공지를 허용하거나 차단합니다.")
    @app_commands.describe(허용="허용하면 호스트 공지를 받습니다.")
    async def _allow_host_announce(self, inter: discord.Interaction, 허용: bool):
        await run_db(self.settings.set_allow_host_announce, inter.guild_id, 허용)
        await inter.response.send_message(
            f"✅ 파티 호스트 공지를 {'허용' if 허용 else '차단'}했습니다.", ephemeral=True
        )

    @설정.command(name="확인", description="현재 서버의 설정을 봅니다.")
    async def _show(self, inter: discord.Interaction):
        party = await run_db(self.settings.get_party_channel, inter.guild_id)
        allow_host_announce = await run_db(
            self.settings.get_allow_host_announce, inter.guild_id
        )

        embed = discord.Embed(title="⚙️ 이 서버의 봇 설정", color=discord.Color.blurple())
        embed.add_field(
            name="파티 패널 채널",
            value=f"<#{party}>" if party else "미지정 — `/설정 파티채널`",
            inline=False,
        )
        embed.add_field(
            name="파티 호스트 공지",
            value="허용" if allow_host_announce else "차단",
            inline=False,
        )
        await inter.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildSettingsCog(bot))
