from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from module.database import UsageRepository, create_usage_repository, run_db


class MemberProfileCog(commands.Cog):
    """서버 프로필 조회 명령을 담는다.

    금지어 카운트는 usage_cog가 쓰는 것과 같은 users 테이블에 있지만, 여기서는
    리포지토리를 직접 주입받아 읽는다. cog 간 런타임 조회를 하나 더 만들지 않기
    위해서다. 이 확장은 환경변수를 요구하지 않으므로 항상 로드된다.
    """

    def __init__(
        self,
        bot: commands.Bot,
        usage_repository: Optional[UsageRepository] = None,
    ):
        self.bot = bot
        self.usage_repository = usage_repository or create_usage_repository()

    @app_commands.command(name="프로필", description="사용자의 서버 프로필 정보를 확인합니다.")
    @app_commands.describe(유저="프로필을 확인할 유저 (선택사항, 기본값: 본인)")
    @app_commands.guild_only()
    async def _profile(
        self, interaction: discord.Interaction, 유저: discord.Member = None
    ):
        profile_member = 유저 if 유저 else interaction.user

        forbidden_count = await run_db(
            self.usage_repository.get_forbidden_count,
            interaction.guild_id,
            profile_member.id,
        )

        # Join date
        joined_date = (
            profile_member.joined_at.strftime("%Y-%m-%d")
            if profile_member.joined_at
            else "알 수 없음"
        )

        embed = discord.Embed(
            title=f"👤 {profile_member.display_name}님의 프로필",
            color=profile_member.color
        )

        if profile_member.avatar:
            embed.set_thumbnail(url=profile_member.avatar.url)

        embed.add_field(name="📅 서버 가입일", value=joined_date, inline=True)
        embed.add_field(name="🚫 금지어 경고", value=f"{forbidden_count}회", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberProfileCog(bot))
