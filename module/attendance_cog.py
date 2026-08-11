import random
from datetime import datetime, timedelta, timezone
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from module.database import AttendanceRepository, create_attendance_repository, run_db

KST = timezone(timedelta(hours=9))


class AttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repository: Optional[AttendanceRepository] = None):
        self.bot = bot
        # DB 접근은 Repository에 위임 (기본: DB_BACKEND 환경 변수에 따라 생성, 테스트 시 주입 가능)
        self.db = repository or create_attendance_repository()

    # ── 다른 Cog에서 사용하는 공개 메서드 (Repository 위임) ── #
    #
    # 전부 async다. 리포지토리는 동기 blocking이므로 run_db()로 스레드에 넘긴다.
    # 이벤트 루프에서 직접 호출하면 백업과의 락 경합 때 봇 전체가 멈춘다.
    # 포인트는 guild_id로 격리되고 AI 일일 사용량은 인스턴스 전역이다.

    async def get_points(self, guild_id: int, user_id: int) -> int:
        """Returns the current points of a user in that guild."""
        return await run_db(self.db.get_points, guild_id, user_id)

    async def deduct_points(
        self, guild_id: int, user_id: int, amount: int, reason: str = "unspecified"
    ) -> bool:
        """Deducts points from a user. Returns True if successful, False if insufficient funds."""
        return await run_db(self.db.deduct_points, guild_id, user_id, amount, reason)

    async def add_points(
        self, guild_id: int, user_id: int, amount: int, reason: str = "unspecified"
    ) -> None:
        """Adds points to a user (internal use)."""
        await run_db(self.db.add_points, guild_id, user_id, amount, reason)

    async def get_ledger(self, guild_id: int, user_id: int, limit: int = 20):
        """Returns recent point movements [(delta, reason, created_at), ...]."""
        return await run_db(self.db.get_ledger, guild_id, user_id, limit)

    async def reserve_ai_usage(
        self, user_id: int, command: str, limit: int
    ) -> Optional[tuple[str, int]]:
        usage_date = datetime.now(KST).date().isoformat()
        count = await run_db(
            self.db.consume_ai_usage, user_id, usage_date, command, limit
        )
        return (usage_date, count) if count is not None else None

    async def release_ai_usage(
        self, user_id: int, usage_date: str, command: str
    ) -> bool:
        return await run_db(
            self.db.release_ai_usage, user_id, usage_date, command
        )

    async def get_ai_usage(self, user_id: int, command: str) -> int:
        usage_date = datetime.now(KST).date().isoformat()
        return await run_db(self.db.get_ai_usage, user_id, usage_date, command)

    async def increment_forbidden_count(self, guild_id: int, user_id: int) -> None:
        """Increments the forbidden word count for a user."""
        await run_db(self.db.increment_forbidden_count, guild_id, user_id)

    async def get_forbidden_count(self, guild_id: int, user_id: int) -> int:
        """Returns the forbidden word count for a user."""
        return await run_db(self.db.get_forbidden_count, guild_id, user_id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """봇이 서버에서 제거되면 그 서버의 포인트·원장을 남기지 않는다."""
        await run_db(self.db.delete_guild, guild.id)

    # ── Slash Commands ── #

    @app_commands.command(name="출석", description="매일 한 번 출석체크하고 랜덤 포인트를 받으세요!")
    @app_commands.guild_only()
    async def _attend(self, inter: discord.Interaction):
        user_id = inter.user.id
        today_str = datetime.now(KST).date().isoformat()

        reward = random.randint(5000, 30000)
        new_points = await run_db(
            self.db.claim_attendance, inter.guild_id, user_id, reward, today_str, "attendance"
        )
        if new_points is None:
            await inter.response.send_message(f"🛑 {inter.user.mention}, 오늘은 이미 출석하셨어요! 내일 또 오세요~", ephemeral=True)
            return

        embed = discord.Embed(
            title="📅 출석체크 완료!",
            description=f"**{reward:,}** 포인트를 획득하셨습니다! 🎉",
            color=0x2ecc71 # Green
        )
        embed.add_field(name="현재 포인트", value=f"{new_points:,} P", inline=False)
        embed.set_footer(text=f"{inter.user.display_name}님의 지갑이 두둑해졌어요!")

        await inter.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="지갑", description="이 서버에서의 내 포인트 잔액을 확인합니다.")
    @app_commands.guild_only()
    async def _wallet(self, inter: discord.Interaction):
        points = await self.get_points(inter.guild_id, inter.user.id)

        embed = discord.Embed(
            title="💰 내 지갑",
            description=f"{inter.user.mention}님의 이 서버에서의 자산입니다.",
            color=0xf1c40f # Gold
        )
        embed.add_field(name="보유 포인트", value=f"**{points:,}** P", inline=False)

        await inter.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="랭킹", description="이 서버의 포인트 부자 TOP 5를 보여줍니다.")
    @app_commands.guild_only()
    async def _ranking(self, inter: discord.Interaction):
        rows = await run_db(self.db.get_top_rankings, inter.guild_id, 5)

        if not rows:
            await inter.response.send_message("아직 랭킹에 등록된 유저가 없어요!", ephemeral=True)
            return

        embed = discord.Embed(title="🏆 명예의 전당 (TOP 5)", color=0xf1c40f)

        for idx, (user_id, points) in enumerate(rows, 1):
            # get_user는 캐시 기반이라 미스 시 원시 ID가 노출된다. 길드 멤버(닉네임)를 우선한다.
            member = inter.guild.get_member(user_id) if inter.guild else None
            if member is None:
                user = self.bot.get_user(user_id) if self.bot else None
                name = user.display_name if user else "알 수 없는 유저"
            else:
                name = member.display_name
            medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][idx-1]
            embed.add_field(name=f"{medal} {name}", value=f"**{points:,}** P", inline=False)

        await inter.response.send_message(embed=embed)

    @app_commands.command(name="프로필", description="사용자의 서버 프로필 정보를 확인합니다.")
    @app_commands.describe(유저="프로필을 확인할 유저 (선택사항, 기본값: 본인)")
    @app_commands.guild_only()
    async def _profile(self, inter: discord.Interaction, 유저: discord.Member = None):
        target_user = 유저 if 유저 else inter.user

        # Get data
        points = await self.get_points(inter.guild_id, target_user.id)
        forbidden_count = await self.get_forbidden_count(inter.guild_id, target_user.id)

        # Join date
        join_date = target_user.joined_at.strftime("%Y-%m-%d") if target_user.joined_at else "알 수 없음"

        embed = discord.Embed(
            title=f"👤 {target_user.display_name}님의 프로필",
            color=target_user.color
        )

        if target_user.avatar:
            embed.set_thumbnail(url=target_user.avatar.url)

        embed.add_field(name="📅 서버 가입일", value=join_date, inline=True)
        embed.add_field(name="💰 보유 포인트", value=f"{points:,} P", inline=True)
        embed.add_field(name="🚫 금지어 경고", value=f"{forbidden_count}회", inline=True)

        await inter.response.send_message(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AttendanceCog(bot))
