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


class GuildSettingsCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, settings: Optional[GuildSettingsRepository] = None
    ):
        self.bot = bot
        self.settings = settings or create_guild_settings_repository()

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

    @설정.command(name="모집채널", description="파티 모집 명령을 사용할 채널을 지정합니다.")
    @app_commands.describe(채널="비워두면 현재 채널로 지정됩니다.")
    async def _recruit(
        self, inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None
    ):
        target = 채널 or inter.channel
        await run_db(self.settings.set_recruit_channel, inter.guild_id, target.id)
        await inter.response.send_message(
            f"✅ 파티 모집 채널을 {target.mention} 으로 지정했습니다.", ephemeral=True
        )

    @설정.command(name="이벤트채널", description="이벤트 조회 명령을 사용할 채널을 지정합니다.")
    @app_commands.describe(채널="비워두면 현재 채널로 지정됩니다.")
    async def _event(
        self, inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None
    ):
        target = 채널 or inter.channel
        await run_db(self.settings.set_event_channel, inter.guild_id, target.id)
        await inter.response.send_message(
            f"✅ 이벤트 채널을 {target.mention} 으로 지정했습니다.", ephemeral=True
        )

    @설정.command(name="확인", description="현재 서버의 설정을 봅니다.")
    async def _show(self, inter: discord.Interaction):
        recruit = await run_db(self.settings.get_recruit_channel, inter.guild_id)
        event = await run_db(self.settings.get_event_channel, inter.guild_id)

        embed = discord.Embed(title="⚙️ 이 서버의 봇 설정", color=discord.Color.blurple())
        embed.add_field(
            name="파티 모집 채널",
            value=f"<#{recruit}>" if recruit else "미지정 — `/설정 모집채널`",
            inline=False,
        )
        embed.add_field(
            name="이벤트 채널",
            value=f"<#{event}>" if event else "미지정 — `/설정 이벤트채널`",
            inline=False,
        )
        await inter.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildSettingsCog(bot))
