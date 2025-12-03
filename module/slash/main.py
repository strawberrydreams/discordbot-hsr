import discord
from discord.ext import commands
from module.slash.config import DISCORD_TOKEN

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
            "module.slash.eventnotify_module",
            "module.slash.makeparty_module",
            "module.slash.forbidfilter_module",
            "module.slash.hyacine_gpt_5_module",
            "module.slash.hyacine_gemini_module",
            "module.slash.attendance_module"
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
        activity = discord.Activity(type=discord.ActivityType.listening, name="Spotify")
        await self.change_presence(status=discord.Status.online, activity=activity)

bot = MyBot()

if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        print("❌ DISCORD_TOKEN not found in environment variables.")
