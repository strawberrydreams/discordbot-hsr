import fcntl
from pathlib import Path
from typing import BinaryIO

import discord
from discord.ext import commands
from module.backup import DATABASES, pid_file, verify_database
from module.config import (
    BACKUP_DIR,
    DATA_DIR,
    DISCORD_GUILD_ID,
    DISCORD_TOKEN,
    PROJECT_ROOT,
    validate_config,
)

# 데이터가 아니라 봇 설치 상태이므로 백업 대상 밖(runtime/)에 둔다.
# DATA_DIR에 두면 백업에서 새 데이터 디렉터리로 복구할 때 전역 클리어가 한 번 더 돈다.
GLOBAL_CLEANUP_MARKER = PROJECT_ROOT / "runtime" / ".global-commands-cleared"

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
        # 슬래시 명령은 길드 sync로 격리되지만, 남아 있는 등록분이 다른 길드에서 뜰 수 있다.
        async def _guild_only_check(interaction: discord.Interaction) -> bool:
            return interaction.guild_id == DISCORD_GUILD_ID

        self.tree.interaction_check = _guild_only_check

        _verify_databases(existing_only=True)

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            print(f"🧩 Loaded extension: {extension}")

        _verify_databases()
        guild = discord.Object(id=DISCORD_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        global_cleanup_marker = GLOBAL_CLEANUP_MARKER
        legacy_marker = DATA_DIR / ".global-commands-cleared"
        if legacy_marker.exists() and not global_cleanup_marker.exists():
            global_cleanup_marker.parent.mkdir(parents=True, exist_ok=True)
            legacy_marker.replace(global_cleanup_marker)  # 재-sync를 피한다
        if not global_cleanup_marker.exists():
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            global_cleanup_marker.parent.mkdir(parents=True, exist_ok=True)
            global_cleanup_marker.touch()
        await self.tree.sync(guild=guild)
        print(f"🔄 Command tree synced to guild {DISCORD_GUILD_ID}")

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
