import discord
from discord.ext import commands
from module.slash.config import DISCORD_TOKEN

# 실행 커맨드: python -m module.slash.main

# Intents 설정
intents = discord.Intents.default()
intents.guild_scheduled_events = True
intents.message_content = True
intents.members = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!", # Slash command 위주지만 prefix 설정은 필요함
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        # Load Extensions (Cogs)
        extensions = [
            "module.slash.eventnotice_cog",
            "module.slash.playwith_cog",
            "module.slash.forbiddenfilter_cog",
            "module.slash.hyacine_chat_cog",
            "module.slash.hyacine_image_cog",
            "module.slash.attendance_cog",
            "module.slash.finance_cog"
        ]
        
        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"🧩 Loaded extension: {ext}")
            except Exception as e:
                print(f"❌ Failed to load extension {ext}: {e}")

        # Sync commands
        # Note: Syncing globally can take up to an hour. For development, sync to specific guild.
        # await self.tree.sync(guild=discord.Object(id=...)) 
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

bot = MyBot()

if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        print("❌ DISCORD_TOKEN not found in environment variables.")
