import fcntl
from pathlib import Path
from typing import BinaryIO

import discord
from discord.ext import commands

from module.backup import DATABASES, verify_database
from module.config import (
    ADMIN_TOKEN,
    BACKUP_DIR,
    DATA_DIR,
    DISCORD_TOKEN,
    GOOGLE_API_KEY,
    OPENAI_API_KEY,
    validate_config,
)

# 실행 커맨드: python -m module.main

# Intents 설정
BOT_INTENTS = discord.Intents.default()
BOT_INTENTS.guild_scheduled_events = True
BOT_INTENTS.message_content = True
BOT_INTENTS.members = True

# (확장 경로, 그 확장이 요구하는 환경변수 이름들)
EXTENSIONS = (
    ("module.webadmin_cog", ("ADMIN_TOKEN",)),
    ("module.guildsettings_cog", ()),
    ("module.eventnotice_cog", ()),
    ("module.playwith_cog", ()),
    ("module.forbiddenfilter_cog", ()),
    ("module.greeting_cog", ()),
    ("module.hyacine_chat_cog", ("OPENAI_API_KEY",)),
    ("module.hyacine_image_cog", ("GOOGLE_API_KEY",)),
    ("module.usage_cog", ()),
    ("module.profile_cog", ()),
)

OPTIONAL_DEPENDENCY_VALUES = {
    "ADMIN_TOKEN": ADMIN_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "GOOGLE_API_KEY": GOOGLE_API_KEY,
}


def available_extensions() -> list[str]:
    """키가 갖춰진 확장만 돌려준다.

    키가 없는 확장은 어차피 동작할 수 없다. 로드해서 런타임에 터지게 두는 것보다
    건너뛰고 로그를 남기는 편이 낫다. 기능 선택 토글이 아니라 의존성 가드다.
    """
    available = []
    for extension, required_environment_variables in EXTENSIONS:
        missing = [
            key
            for key in required_environment_variables
            if not OPTIONAL_DEPENDENCY_VALUES.get(key)
        ]
        if missing:
            print(f"⏭️ Skipped: {extension} ({', '.join(missing)} 없음)")
            continue
        available.append(extension)
    return available


def _verify_databases(
    existing_only: bool = False,
    allow_legacy: bool = False,
) -> None:
    for filename, required_tables in DATABASES.items():
        database_path = DATA_DIR / filename
        if existing_only and not database_path.exists():
            continue
        verify_database(
            database_path,
            required_tables,
            source_name=filename,
            allow_legacy=allow_legacy,
        )


def acquire_instance_lock(lock_path: Path) -> BinaryIO:
    lock_file = lock_path.open("a+b")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError(f"봇이 이미 실행 중입니다: {lock_path}") from exc
    return lock_file


class HyacineBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!", # Slash command 위주지만 prefix 설정은 필요함
            intents=BOT_INTENTS,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def setup_hook(self):
        _verify_databases(existing_only=True, allow_legacy=True)

        for extension in available_extensions():
            await self.load_extension(extension)
            print(f"🧩 Loaded extension: {extension}")

        _verify_databases()
        # 공개 배포 봇이므로 전역 sync다. 길드 sync는 봇이 설치된 서버를 미리
        # 알아야 하는데, 초대는 언제든 일어난다. 데이터 격리는 스키마가 보장한다.
        await self.tree.sync()
        print("🔄 Command tree synced globally")

    async def close(self):
        web_admin_cog = self.get_cog("WebAdminCog")
        try:
            if web_admin_cog is not None:
                await web_admin_cog.close()
        finally:
            await super().close()

    async def on_ready(self):
        print(f"✅ {self.user} 봇이 실행되었습니다!")

        presence_activity = discord.Game(name="📝 생각나는 아이디어를 끄적이는 중...")
        # activity = discord.Streaming(name="broadcast_title", url="broadcast_link")
        # activity = discord.Activity(type=discord.ActivityType.watching, name="video_title")

        await self.change_presence(
            status=discord.Status.online, activity=presence_activity
        )
        # await client.change_presence(status=discord.Status.idle, activity=activity)
        # await client.change_presence(status=discord.Status.dnd, activity=activity)
        # await client.change_presence(status=discord.Status.invisible, activity=activity)

def main() -> None:
    validate_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with acquire_instance_lock(DATA_DIR / ".bot.lock"):
        HyacineBot().run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
