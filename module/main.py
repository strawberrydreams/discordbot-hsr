import fcntl
from pathlib import Path
from typing import BinaryIO

import discord
from discord.ext import commands
from module.backup import DATABASES, pid_file, verify_database
from module.config import (
    BACKUP_DIR,
    DATA_DIR,
    DISCORD_TOKEN,
    validate_config,
)

# 실행 커맨드: python -m module.main

# Intents 설정
intents = discord.Intents.default()
intents.guild_scheduled_events = True
intents.message_content = True
intents.members = True

EXTENSIONS = (
    "module.guildsettings_cog",
    "module.eventnotice_cog",
    "module.playwith_cog",
    "module.forbiddenfilter_cog",
    "module.hyacine_chat_cog",
    "module.hyacine_image_cog",
    "module.attendance_cog",
    "module.finance_cog",
)


def _verify_databases(existing_only: bool = False) -> None:
    for filename, required_tables in DATABASES.items():
        path = DATA_DIR / filename
        if existing_only and not path.exists():
            continue
        verify_database(path, required_tables)


def acquire_instance_lock(path: Path) -> BinaryIO:
    lock = path.open("a+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError(f"봇이 이미 실행 중입니다: {path}") from exc
    return lock


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!", # Slash command 위주지만 prefix 설정은 필요함
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def setup_hook(self):
        _verify_databases(existing_only=True)

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            print(f"🧩 Loaded extension: {extension}")

        _verify_databases()
        # 공개 배포 봇이므로 전역 sync다. 길드 sync는 봇이 설치된 서버를 미리
        # 알아야 하는데, 초대는 언제든 일어난다. 데이터 격리는 스키마가 보장한다.
        await self.tree.sync()
        print("🔄 Command tree synced globally")

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
    with acquire_instance_lock(DATA_DIR / ".bot.lock"), \
         pid_file(DATA_DIR / ".bot.pid"):
        MyBot().run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
