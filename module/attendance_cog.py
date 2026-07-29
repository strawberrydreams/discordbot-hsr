import random
from datetime import datetime, timedelta, timezone
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from module.database import AttendanceRepository, create_attendance_repository

class AttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repository: Optional[AttendanceRepository] = None):
        self.bot = bot
        # DB 접근은 Repository에 위임 (기본: DB_BACKEND 환경 변수에 따라 생성, 테스트 시 주입 가능)
        self.db = repository or create_attendance_repository()

    # ── 다른 Cog에서 사용하는 공개 메서드 (Repository 위임) ── #

    def get_points(self, user_id: int) -> int:
        """Returns the current points of a user."""
        return self.db.get_points(user_id)

    def deduct_points(self, user_id: int, amount: int) -> bool:
        """Deducts points from a user. Returns True if successful, False if insufficient funds."""
        return self.db.deduct_points(user_id, amount)

    def add_points(self, user_id: int, amount: int):
        """Adds points to a user (internal use)."""
        self.db.add_points(user_id, amount)

    def increment_forbidden_count(self, user_id: int):
        """Increments the forbidden word count for a user."""
        self.db.increment_forbidden_count(user_id)

    def get_forbidden_count(self, user_id: int) -> int:
        """Returns the forbidden word count for a user."""
        return self.db.get_forbidden_count(user_id)

    # ── Slash Commands ── #

    @app_commands.command(name="출석", description="매일 한 번 출석체크하고 랜덤 포인트를 받으세요!")
    async def _attend(self, inter: discord.Interaction):
        user_id = inter.user.id
        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).date().isoformat()

        data = self.db.get_attendance(user_id)
        current_points = 0
        last_date = None

        if data:
            current_points, last_date = data

        if last_date == today_str:
            await inter.response.send_message(f"🛑 {inter.user.mention}, 오늘은 이미 출석하셨어요! 내일 또 오세요~", ephemeral=True)
            return

        # Reward calculation
        reward = random.randint(5000, 30000)
        new_points = current_points + reward

        self.db.update_attendance(user_id, new_points, today_str)

        embed = discord.Embed(
            title="📅 출석체크 완료!",
            description=f"**{reward:,}** 포인트를 획득하셨습니다! 🎉",
            color=0x2ecc71 # Green
        )
        embed.add_field(name="현재 포인트", value=f"{new_points:,} P", inline=False)
        embed.set_footer(text=f"{inter.user.display_name}님의 지갑이 두둑해졌어요!")

        await inter.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="지갑", description="내 포인트 잔액을 확인합니다.")
    async def _wallet(self, inter: discord.Interaction):
        points = self.db.get_points(inter.user.id)

        embed = discord.Embed(
            title="💰 내 지갑",
            description=f"{inter.user.mention}님의 현재 자산입니다.",
            color=0xf1c40f # Gold
        )
        embed.add_field(name="보유 포인트", value=f"**{points:,}** P", inline=False)

        await inter.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="럭키박스", description="포인트를 걸고 20% ~ 300% 대박을 노려보세요! (확률 랜덤)")
    @app_commands.describe(금액="베팅할 포인트 금액")
    async def _luckybox(self, inter: discord.Interaction, 금액: int):
        if 금액 <= 0:
            await inter.response.send_message("❌ 0보다 큰 금액을 걸어야죠!", ephemeral=True)
            return

        user_id = inter.user.id
        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).date().isoformat()

        multiplier = random.uniform(0.2, 3.0)
        status, result = self.db.play_luckybox(user_id, 금액, today_str, multiplier)

        if status == "limit":
            await inter.response.send_message(f"🛑 {inter.user.mention}, 오늘은 그만! 하루 3번만 가능해요. 내일 다시 도전하세요! 🎲", ephemeral=True)
            return

        if status == "insufficient":
            await inter.response.send_message(f"❌ 포인트가 부족해요! (보유: {result['points']:,} P)", ephemeral=True)
            return

        result_amount = result["result_amount"]
        final_points = result["final_points"]
        profit = result_amount - 금액

        # Visuals
        color = 0x2ecc71 if profit >= 0 else 0xe74c3c
        title = "🎉 대박!" if profit >= 0 else "😭 쪽박..."
        desc = f"**{multiplier:.0%}**를 뽑으셨네요!\n"

        if profit >= 0:
            desc += f"투자금 **{금액:,}** P ➡️ 획득 **{result_amount:,}** P (+{profit:,})"
        else:
            desc += f"투자금 **{금액:,}** P ➡️ 획득 **{result_amount:,}** P ({profit:,})"

        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text=f"현재 잔액: {final_points:,} P")

        await inter.response.send_message(embed=embed)

    @app_commands.command(name="랭킹", description="포인트 부자 TOP 5를 보여줍니다.")
    async def _ranking(self, inter: discord.Interaction):
        rows = self.db.get_top_rankings(limit=5)

        if not rows:
            await inter.response.send_message("아직 랭킹에 등록된 유저가 없어요!", ephemeral=True)
            return

        embed = discord.Embed(title="🏆 명예의 전당 (TOP 5)", color=0xf1c40f)

        for idx, (user_id, points) in enumerate(rows, 1):
            user = self.bot.get_user(user_id)
            name = user.display_name if user else f"Unknown User ({user_id})"
            medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][idx-1]
            embed.add_field(name=f"{medal} {name}", value=f"**{points:,}** P", inline=False)

        await inter.response.send_message(embed=embed)

    @app_commands.command(name="프로필", description="사용자의 서버 프로필 정보를 확인합니다.")
    @app_commands.describe(유저="프로필을 확인할 유저 (선택사항, 기본값: 본인)")
    async def _profile(self, inter: discord.Interaction, 유저: discord.Member = None):
        target_user = 유저 if 유저 else inter.user

        # Get data
        points = self.db.get_points(target_user.id)
        forbidden_count = self.db.get_forbidden_count(target_user.id)

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
