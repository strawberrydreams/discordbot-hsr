import discord
from discord.ext import commands
from module.backup import verify_database
from module.config import BACKUP_DIR, DATA_DIR, DISCORD_TOKEN, validate_config

# 실행 커맨드: python -m module.main

# Intents 설정
intents = discord.Intents.default()
intents.guild_scheduled_events = True
intents.message_content = True
intents.members = True

EXTENSIONS = (
    "module.eventnotice_cog",
    "module.playwith_cog",
    "module.forbiddenfilter_cog",
    "module.hyacine_chat_cog",
    "module.hyacine_image_cog",
    "module.attendance_cog",
    "module.finance_cog",
)

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!", # Slash command 위주지만 prefix 설정은 필요함
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            print(f"🧩 Loaded extension: {extension}")

        verify_database(DATA_DIR / "attendance_data.db", {"users"})
        verify_database(DATA_DIR / "party_data.db", {"parties", "participants"})
        await self.tree.sync()
        print("🔄 Command tree synced")

    async def on_ready(self):
        print(f"✅ {self.user} 봇이 실행되었습니다!")

        activity = discord.Game(name="📝 생각나는 아이디어를 끄적이는 중...")
        # activity = discord.Streaming(name="broadcast_title", url="broadcast_link")
        # activity = discord.Activity(type=discord.ActivityType.listening, name="music_title")
        # activity = discord.Activity(type=discord.ActivityType.watching, name="video_title")

        await self.change_presence(status=discord.Status.online, activity=activity)
        # await client.change_presence(status=discord.Status.idle, activity=activity)
        # await client.change_presence(status=discord.Status.dnd, activity=activity)
        # await client.change_presence(status=discord.Status.invisible, activity=activity)

def main() -> None:
    validate_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    MyBot().run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
