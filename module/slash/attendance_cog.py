import random
import sqlite3
from datetime import date
import discord
from discord import app_commands
from discord.ext import commands

from module.slash.config import DATA_DIR

DB_FILE = DATA_DIR / "attendance_data.db"

class AttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    points INTEGER DEFAULT 0,
                    last_attendance_date TEXT,
                    forbidden_count INTEGER DEFAULT 0
                )
            """)
            
            # Migration for existing tables
            try:
                c.execute("ALTER TABLE users ADD COLUMN forbidden_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass # Column already exists
                
            conn.commit()

    def get_points(self, user_id: int) -> int:
        """Returns the current points of a user."""
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
            result = c.fetchone()
            return result[0] if result else 0

    def deduct_points(self, user_id: int, amount: int) -> bool:
        """Deducts points from a user. Returns True if successful, False if insufficient funds."""
        current = self.get_points(user_id)
        if current < amount:
            return False
        
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET points = points - ? WHERE user_id = ?", (amount, user_id))
            conn.commit()
        return True

    def add_points(self, user_id: int, amount: int):
        """Adds points to a user (internal use)."""
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO users (user_id, points) VALUES (?, 0)", (user_id,))
            c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (amount, user_id))
            conn.commit()

    def _get_user_data(self, user_id: int):
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT points, last_attendance_date FROM users WHERE user_id = ?", (user_id,))
            return c.fetchone()

    def increment_forbidden_count(self, user_id: int):
        """Increments the forbidden word count for a user."""
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            # Ensure user exists
            c.execute("INSERT OR IGNORE INTO users (user_id, points, forbidden_count) VALUES (?, 0, 0)", (user_id,))
            c.execute("UPDATE users SET forbidden_count = forbidden_count + 1 WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_forbidden_count(self, user_id: int) -> int:
        """Returns the forbidden word count for a user."""
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT forbidden_count FROM users WHERE user_id = ?", (user_id,))
            result = c.fetchone()
            return result[0] if result else 0

    def _update_user_data(self, user_id: int, points: int, attendance_date: str):
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            # Check if user exists
            c.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            exists = c.fetchone()
            
            if exists:
                c.execute("""
                    UPDATE users 
                    SET points = ?, last_attendance_date = ? 
                    WHERE user_id = ?
                """, (points, attendance_date, user_id))
            else:
                c.execute("""
                    INSERT INTO users (user_id, points, last_attendance_date, forbidden_count)
                    VALUES (?, ?, ?, 0)
                """, (user_id, points, attendance_date))
            conn.commit()

    @app_commands.command(name="출석", description="매일 한 번 출석체크하고 랜덤 포인트를 받으세요!")
    async def _attend(self, inter: discord.Interaction):
        user_id = inter.user.id
        today_str = date.today().isoformat()
        
        data = self._get_user_data(user_id)
        current_points = 0
        last_date = None
        
        if data:
            current_points, last_date = data
        
        if last_date == today_str:
            await inter.response.send_message(f"🛑 {inter.user.mention}, 오늘은 이미 출석하셨어요! 내일 또 오세요~", ephemeral=True)
            return
        
        # Reward calculation
        reward = random.randint(1000, 50000)
        new_points = current_points + reward
        
        self._update_user_data(user_id, new_points, today_str)
        
        embed = discord.Embed(
            title="📅 출석체크 완료!",
            description=f"**{reward:,}** 포인트를 획득하셨습니다! 🎉",
            color=0x2ecc71 # Green
        )
        embed.add_field(name="현재 포인트", value=f"{new_points:,} P", inline=False)
        embed.set_footer(text=f"{inter.user.display_name}님의 지갑이 두둑해졌어요!")
        
        await inter.response.send_message(embed=embed)

    @app_commands.command(name="지갑", description="내 포인트 잔액을 확인합니다.")
    async def _wallet(self, inter: discord.Interaction):
        points = self.get_points(inter.user.id)
        
        embed = discord.Embed(
            title="💰 내 지갑",
            description=f"{inter.user.mention}님의 현재 자산입니다.",
            color=0xf1c40f # Gold
        )
        embed.add_field(name="보유 포인트", value=f"**{points:,}** P", inline=False)
        
        await inter.response.send_message(embed=embed)

    @app_commands.command(name="럭키박스", description="포인트를 걸고 20% ~ 300% 대박을 노려보세요! (확률 랜덤)")
    @app_commands.describe(금액="베팅할 포인트 금액")
    async def _luckybox(self, inter: discord.Interaction, 금액: int):
        if 금액 <= 0:
            await inter.response.send_message("❌ 0보다 큰 금액을 걸어야죠!", ephemeral=True)
            return

        current_points = self.get_points(inter.user.id)
        if current_points < 금액:
            await inter.response.send_message(f"❌ 포인트가 부족해요! (보유: {current_points:,} P)", ephemeral=True)
            return

        # Deduct bet first
        self.deduct_points(inter.user.id, 금액)
        
        # Calculate result
        multiplier = random.uniform(0.2, 3.0)
        result_amount = int(금액 * multiplier)
        profit = result_amount - 금액
        
        # Add result
        self.add_points(inter.user.id, result_amount)
        final_points = self.get_points(inter.user.id)

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
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, points FROM users ORDER BY points DESC LIMIT 5")
            rows = c.fetchall()

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
        points = self.get_points(target_user.id)
        forbidden_count = self.get_forbidden_count(target_user.id)
        
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
        
        await inter.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(AttendanceCog(bot))
