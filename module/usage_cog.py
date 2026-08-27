from datetime import datetime, timedelta, timezone
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from module.database import UsageRepository, create_usage_repository, run_db

KST_TIMEZONE = timezone(timedelta(hours=9))


class UsageCog(commands.Cog):
    """AI 일일 한도와 금지어 경고 횟수를 보관한다.

    이 확장은 환경변수를 요구하지 않으므로 항상 로드된다. 길드 이탈 시 데이터
    삭제(on_guild_remove)가 여기 달려 있으므로, AI 키 게이트 뒤로 옮기면 안 된다.
    """

    def __init__(
        self,
        bot: commands.Bot,
        usage_repository: Optional[UsageRepository] = None,
    ):
        self.bot = bot
        # DB 접근은 Repository에 위임 (기본: DB_BACKEND 환경 변수에 따라 생성, 테스트 시 주입 가능)
        self.usage_repository = usage_repository or create_usage_repository()

    # ── 다른 Cog에서 사용하는 공개 메서드 (Repository 위임) ── #
    #
    # 전부 async다. 리포지토리는 동기 blocking이므로 run_db()로 스레드에 넘긴다.
    # 이벤트 루프에서 직접 호출하면 백업과의 락 경합 때 봇 전체가 멈춘다.
    # 금지어 카운트는 guild_id로 격리되고 AI 일일 사용량은 인스턴스 전역이다.

    async def reserve_ai_usage(
        self, user_id: int, usage_category: str, daily_limit: int
    ) -> Optional[tuple[str, int]]:
        usage_date = datetime.now(KST_TIMEZONE).date().isoformat()
        usage_count = await run_db(
            self.usage_repository.consume_ai_usage,
            user_id,
            usage_date,
            usage_category,
            daily_limit,
        )
        return (usage_date, usage_count) if usage_count is not None else None

    async def release_ai_usage(
        self, user_id: int, usage_date: str, usage_category: str
    ) -> bool:
        return await run_db(
            self.usage_repository.release_ai_usage,
            user_id,
            usage_date,
            usage_category,
        )

    async def get_ai_usage(self, user_id: int, usage_category: str) -> int:
        usage_date = datetime.now(KST_TIMEZONE).date().isoformat()
        return await run_db(
            self.usage_repository.get_ai_usage,
            user_id,
            usage_date,
            usage_category,
        )

    async def increment_forbidden_count(self, guild_id: int, user_id: int) -> None:
        """Increments the forbidden word count for a user."""
        await run_db(
            self.usage_repository.increment_forbidden_count, guild_id, user_id
        )

    async def get_forbidden_count(self, guild_id: int, user_id: int) -> int:
        """Returns the forbidden word count for a user."""
        return await run_db(
            self.usage_repository.get_forbidden_count, guild_id, user_id
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """봇이 서버에서 제거되면 그 서버의 금지어 카운트를 남기지 않는다."""
        await run_db(self.usage_repository.delete_guild, guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """멤버가 나가면 해당 서버의 개인별 금지어 기록을 남기지 않는다."""
        await run_db(
            self.usage_repository.delete_user,
            member.guild.id,
            member.id,
        )

    # ── Slash Commands ── #

    @app_commands.command(name="프로필", description="사용자의 서버 프로필 정보를 확인합니다.")
    @app_commands.describe(유저="프로필을 확인할 유저 (선택사항, 기본값: 본인)")
    @app_commands.guild_only()
    async def _profile(
        self, interaction: discord.Interaction, 유저: discord.Member = None
    ):
        profile_member = 유저 if 유저 else interaction.user

        forbidden_count = await self.get_forbidden_count(
            interaction.guild_id, profile_member.id
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
    await bot.add_cog(UsageCog(bot))
