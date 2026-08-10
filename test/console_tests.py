# 콘솔 전용 테스트 (디스코드 연결 불필요)
# 실행: python -m test.console_tests (프로젝트 루트에서)
#
# 검증 항목:
#   1. SQLite 마이그레이션 (구버전 users 테이블에 luckybox 컬럼 추가)
#   2. deduct_points 원자성 (동시 차감 시 잔액이 음수가 되지 않음)
#   3. 포인트 원장 (모든 이동 기록, 실패한 차감은 미기록)
#   4. PartyRepository CRUD (파티 생성/참가/탈퇴/만료 정리)
#   5. Repository 팩토리 (sqlite 선택, 미지원 백엔드 거부)
#   6. AttendanceCog 파사드 (Repository 주입 및 위임)
#   7. 채널별 대화 세션 분리 (히스토리 독립)
#   8. 전체 모듈 import 스모크 테스트
#   9. 음악 core·영속 패널 계약
#
# 모든 테스트는 임시 디렉터리의 격리된 DB를 사용하므로 운영 데이터를 건드리지 않는다.

import datetime
import asyncio
import gc
import importlib
import inspect
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import dotenv_values

# module.* import 전에 환경 변수를 설정해야 함
_TMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="hsr_test_"))
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ["DATA_DIR"] = str(_TMP_DIR)
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("GOOGLE_API_KEY", "test-dummy")

import module.database as database
from module.database import (
    SQLiteAttendanceRepository,
    SQLitePartyRepository,
    SQLiteGuildSettingsRepository,
    create_attendance_repository,
    create_party_repository,
)
from module.attendance_cog import AttendanceCog
from module.hyacine_chat_cog import HyacineChatCog

PASS = 0
FAIL = 0

def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def _create_legacy_attendance_db(path: pathlib.Path, version: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(f"""
            CREATE TABLE users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                last_attendance_date TEXT,
                forbidden_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE point_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            INSERT INTO users VALUES (7, 8, 9000, NULL, 0);
            INSERT INTO point_ledger
                (guild_id, user_id, delta, reason, created_at)
                VALUES (7, 8, 9000, 'attendance', 1);
            PRAGMA user_version = {version};
        """)



# ─────────── 길드 바인딩 어댑터 ─────────── #
#
# 아래 테스트들이 검증하는 것은 원자성·원장·마이그레이션 의미이지 길드 격리가
# 아니다(격리는 test_guild_isolation이 담당). 매 호출에 guild_id를 적기보다
# 단일 길드에 고정해 원래 의도를 그대로 둔다.

_TEST_GUILD = 1


class _GuildBound:
    """길드 단위 리포지토리 호출에 고정 guild_id를 앞에 끼워 넣는다."""

    # 길드 인자를 받지 않는 메서드(전 길드 대상 또는 길드 자체가 인자).
    _UNBOUND = {
        "consume_ai_usage",
        "delete_expired_parties",
        "delete_guild",
        "get_ai_usage",
        "list_expired_parties",
        "release_ai_usage",
    }

    def __init__(self, repo, guild_id=_TEST_GUILD):
        self._repo = repo
        self._guild_id = guild_id

    def __getattr__(self, name):
        attr = getattr(self._repo, name)
        if not callable(attr) or name in self._UNBOUND:
            return attr

        def bound(*args, **kwargs):
            return attr(self._guild_id, *args, **kwargs)

        return bound


def _bind(repo):
    return _GuildBound(repo)


def test_config_paths():
    import module.config as config

    check("PROJECT_ROOT는 절대 경로", config.PROJECT_ROOT.is_absolute())
    check("DATA_DIR는 절대 경로", config.DATA_DIR.is_absolute())
    check("BACKUP_DIR는 절대 경로", config.BACKUP_DIR.is_absolute())
    check(
        "금지어 예시 파일은 Git 추적",
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "settings/forbidden_words.example.json"],
            cwd=PROJECT_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0,
    )

def test_config_validation():
    import module.config as config

    original = config.DISCORD_TOKEN
    config.DISCORD_TOKEN = None
    try:
        try:
            config.validate_config()
            check("Discord 토큰 누락 거부", False)
        except RuntimeError as exc:
            check("Discord 토큰 누락 거부", "DISCORD_TOKEN" in str(exc))
    finally:
        config.DISCORD_TOKEN = original

    original_values = {
        name: getattr(config, name)
        for name in (
            "DISCORD_TOKEN",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
        )
        if hasattr(config, name)
    }
    try:
        config.DISCORD_TOKEN = "test-token"
        config.OPENAI_API_KEY = None
        config.GOOGLE_API_KEY = None
        # 채널/길드 ID는 더 이상 환경변수가 아니다. 서버 관리자가 /설정으로 지정한다.
        config.validate_config()
        check(
            "채널·길드 ID 없이도 기동 가능",
            not any(
                hasattr(config, name)
                for name in ("RECRUIT_CHANNEL_ID", "EVENT_CHANNEL_ID", "DISCORD_GUILD_ID")
            ),
        )
    finally:
        for name, value in original_values.items():
            setattr(config, name, value)


def test_split_env_loading():
    import module.config as config

    root = _TMP_DIR / "split-env"
    root.mkdir()
    (root / ".env.secrets").write_text(
        "OPENAI_API_KEY=file-secret\n"
        "GOOGLE_API_KEY=file-google\n"
        "OVERLAP_TEST=secrets-win\n",
        encoding="utf-8",
    )
    (root / ".env.runtime").write_text(
        "BACKUP_RETENTION_DAYS=123\n"
        "OVERLAP_TEST=runtime-loses\n",
        encoding="utf-8",
    )

    names = (
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "BACKUP_RETENTION_DAYS",
        "OVERLAP_TEST",
    )
    original = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["GOOGLE_API_KEY"] = "process-wins"
        config._load_env_files(root)
        check("secrets 파일 로드", os.environ["OPENAI_API_KEY"] == "file-secret")
        check("runtime 파일 로드", os.environ["BACKUP_RETENTION_DAYS"] == "123")
        check("프로세스 환경 우선", os.environ["GOOGLE_API_KEY"] == "process-wins")
        check("secrets 파일 우선", os.environ["OVERLAP_TEST"] == "secrets-win")
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_public_env_contract():
    example = dotenv_values(PROJECT_ROOT / ".env.example")
    expected = {
        "DISCORD_TOKEN",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "ADMIN_TOKEN",
        "DATA_DIR",
        "BACKUP_DIR",
        "SETTINGS_DIR",
        "BACKUP_INTERVAL_SECONDS",
        "BACKUP_RETENTION_DAYS",
        "AI_COOLDOWN_SECONDS",
        "DB_BACKEND",
        "CHAT_MODEL_LIGHT",
        "CHAT_MODEL_DEEP",
        "IMAGE_MODEL",
        "LIMIT_LIGHT",
        "LIMIT_DEEP",
        "LIMIT_IMAGE",
    }
    check("공개 env 변수 계약", set(example) == expected)
    check("관리 토큰 env 예제는 빈 값", example["ADMIN_TOKEN"] == "")
    check(
        "실제 env 파일 ignore",
        all(
            subprocess.run(
                ["git", "check-ignore", "-q", name],
                cwd=PROJECT_ROOT,
                check=False,
            ).returncode
            == 0
            for name in (".env", ".env.secrets", ".env.runtime")
        ),
    )
    check(
        "공개 env 예제 추적",
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env.example"],
            cwd=PROJECT_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0,
    )


def test_web_admin_atomic_settings_contract():
    import module.config as config

    settings_dir = _TMP_DIR / "web-admin-settings"
    settings_dir.mkdir()
    target = settings_dir / "forbidden_words.json"
    target.write_bytes(b'["old"]\n')
    with patch.object(config, "SETTINGS_DIR", settings_dir):
        config.atomic_write_settings("forbidden_words.json", ["new"])
        check(
            "관리 설정 정상 원자 교체",
            json.loads(target.read_text(encoding="utf-8")) == ["new"],
        )
        valid_bytes = target.read_bytes()
        try:
            config.atomic_write_settings("forbidden_words.json", {})
            rejected = False
        except ValueError:
            rejected = True
        check(
            "관리 설정 validation 실패 시 원본 보존",
            rejected and target.read_bytes() == valid_bytes,
        )


def test_forbidden_word_document_path():
    public_docs = "\n".join(
        (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "docs/operations.md")
    )
    check("금지어 기준 경로는 settings", "settings/forbidden_words.json" in public_docs)
    check(
        "폐기된 금지어 runtime 경로 없음",
        "runtime/data/forbidden_words.json" not in public_docs,
    )
    check("폐기된 금지어 env 없음", "FORBIDDEN_WORDS_FILE" not in public_docs)


def test_readme_public_distribution_contract():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "자가 호스팅 커뮤니티 유틸리티 봇",
        "자신의 Discord Application과 봇 토큰",
        "Guild Install만",
        "`bot`, `applications.commands`",
        "`View Channel`",
        "`Send Messages`",
        "`Read Message History`",
        "`Embed Links`",
        "`Attach Files`",
        "`Message Content`와 `Server Members`",
        "`Public Bot`을 끄세요",
        "Portal 설정을 변경하지는 않습니다",
        "settings/forbidden_words.json",
        "사용자별 KST 일일 AI 한도",
        "`LIMIT_LIGHT`, `LIMIT_DEEP`, `LIMIT_IMAGE`",
        "provider 계정에도 예산 상한",
        "Docker Compose를 권장",
        "launchd 선택 사항",
        "포인트·출석·금지어 카운트, 파티, 길드 설정 데이터는 `guild_id`",
        "AI 사용량 한도만 사용자별·봇 인스턴스 전역",
        "`Manage Guild` 권한",
    )
    check("README 공개 배포 계약", all(term in readme for term in required))
    check(
        "README는 Hyacine 팬 프로젝트 고지 유지",
        "Hyacine" in readme and "비공식 팬 프로젝트" in readme and "HoYoverse" in readme,
    )
    check("README는 타인 호스팅을 약속하지 않음", "다른 사람에게 봇을 호스팅" in readme)


def test_operations_document_contract():
    operations = (PROJECT_ROOT / "docs/operations.md").read_text(encoding="utf-8")
    restore = operations.split("## 검증된 백업으로 실제 복구", 1)[1].split("## 배포", 1)[0]
    check(
        "복구는 내장 stage restore를 사용",
        "stage_restore(Path(sys.argv[1]), Path(sys.argv[2]))" in restore,
    )
    check("복구가 stage의 모든 DB를 순회", 'for staged in "$RESTORE_STAGE"/*.db' in restore)
    check(
        "복구가 존재한 설정 파일별 확인 교체",
        "for name in persona.json forbidden_words.json games.json; do" in restore
        and 'test -f "$staged" || continue' in restore
        and 'cmp -s "$staged" "settings/$name"' in restore,
    )
    check("복구 문서에 긴 DB 수리 one-liner 없음", "verify_database" not in restore)
    check("원장 예시에 길드 ID 포함", "repo.get_ledger(GUILD_ID, USER_ID" in operations)
    check("잔액 예시에 길드 ID 포함", "repo.get_points(GUILD_ID, USER_ID)" in operations)
    check("AI 한도는 사용자별 인스턴스 전역", "사용자별·봇 인스턴스 전역" in operations)
    check("AI 한도는 KST 자정 리셋", "매일 KST 자정에 리셋" in operations)
    check("AI 한도는 포인트와 별도", "포인트와 별도로 적용" in operations)
    check("provider 계정 예산 안전망 유지", "OpenAI 계정 예산 한도" in operations)


def test_final_installation_and_operations_contract():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    operations = (PROJECT_ROOT / "docs/operations.md").read_text(encoding="utf-8")
    public_docs = f"{readme}\n{operations}"
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    check(
        "설치 문서는 세 설정 JSON 초기 복사를 안내",
        all(
            f"settings/{name}.json" in readme
            and f"settings/{name}.example.json" in readme
            for name in ("persona", "forbidden_words", "games")
        ),
    )
    check(
        "문서는 현재 설정과 영속 panel 명령만 안내",
        all(
            term in public_docs
            for term in ("/설정 시작", "/설정 파티채널", "/설정 음악채널", "/설정 공지허용")
        ),
    )
    check(
        "문서는 channel과 voice 권한을 안내",
        all(term in public_docs for term in ("`Manage Channels`", "`Connect`", "`Speak`")),
    )
    check(
        "웹 관리는 선택 토큰과 고정 loopback 경계를 문서화",
        all(term in public_docs for term in ("`ADMIN_TOKEN`", "`127.0.0.1:8080`", "host-scoped")),
    )
    check(
        "음악 선택 의존성과 yt-dlp 갱신 절차를 문서화",
        "나머지 봇은 동작" in public_docs
        and "pip install --upgrade yt-dlp" in public_docs,
    )
    check(
        "MIT와 무기여 정책을 유지",
        "MIT License" in readme and "사용자 기여를 받지 않습니다" in readme,
    )
    check(
        "사용자 문서에 폐기된 파티 명령·설정 열 없음",
        all(
            term not in public_docs
            for term in (
                "/모집",
                "/파티",
                "/나가기",
                "/변경",
                "recruit_channel_id",
                "event_channel_id",
            )
        ),
    )
    check(
        "백업 문서는 세 DB와 존재한 세 설정 파일을 같은 set으로 설명",
        all(
            term in operations
            for term in (
                "attendance_data.db",
                "party_data.db",
                "guild_settings.db",
                "settings/persona.json",
                "settings/forbidden_words.json",
                "settings/games.json",
            )
        ),
    )
    check(
        "Compose bot 설정은 rw, backup 설정은 ro, web port는 없음",
        "- ./settings:/app/settings\n" in compose
        and "- ./settings:/app/settings:ro" in compose
        and "ports:" not in compose,
    )
    check(
        "Docker runtime은 ffmpeg와 main process healthcheck를 가진다",
        "ffmpeg" in dockerfile
        and "HEALTHCHECK" in dockerfile
        and "b'module.main'" in dockerfile,
    )


def test_deployment_contracts():
    import plistlib

    services = None
    dockerignore = (
        (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    check(
        "Docker build context excludes private settings JSON only",
        "settings/*.env" in dockerignore
        and "settings/*.json" in dockerignore
        and "!settings/*.example.json" in dockerignore
        and dockerignore.index("settings/*.json")
        < dockerignore.index("!settings/*.example.json"),
    )
    docker = shutil.which("docker")
    if docker is not None:
        compose_version = subprocess.run(
            [docker, "compose", "version"],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        if compose_version.returncode == 0:
            compose_result = subprocess.run(
                [
                    docker,
                    "compose",
                    "config",
                    "--no-env-resolution",
                    "--format",
                    "json",
                ],
                cwd=PROJECT_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            services = json.loads(compose_result.stdout)["services"]

    bot_plist = plistlib.loads(
        (
            PROJECT_ROOT / "deploy/macos/com.discordbot.hsr.plist.example"
        ).read_bytes()
    )
    backup_plist = plistlib.loads(
        (
            PROJECT_ROOT / "deploy/macos/com.discordbot.hsr-backup.plist.example"
        ).read_bytes()
    )
    newsyslog_entries = [
        line.split()
        for line in (
            PROJECT_ROOT
            / "deploy/macos/com.discordbot.hsr.newsyslog.conf.example"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    if services is None:
        print(
            "  ⏭️ SKIP: rendered Compose deployment checks "
            "(Docker Compose CLI unavailable)"
        )
    else:
        bot = services["bot"]
        backup = services["backup"]
        check(
            "Compose 이미지는 한 번만 빌드",
            "build" in bot and "build" not in backup,
        )
        check(
            "두 서비스가 같은 이미지 사용",
            bot.get("image") == backup.get("image") == "discordbot-hsr:local",
        )
        check(
            "bot 서비스는 시크릿을 유지",
            [pathlib.Path(item["path"]).name for item in bot["env_file"]]
            == [".env.runtime", ".env.secrets"],
        )
        check(
            "backup 서비스는 시크릿을 받지 않음",
            [pathlib.Path(item["path"]).name for item in backup["env_file"]]
            == [".env.runtime"],
        )
        check(
            # WAL DB는 읽기 전용 연결이라도 -shm/-wal 생성이 필요하므로 :ro면 백업이 실패한다.
            "backup 데이터 마운트는 WAL을 위해 쓰기 가능",
            all(
                any(
                    str(mount.get("source", "")).endswith("runtime/data")
                    and mount.get("target") == "/app/runtime/data"
                    and not mount.get("read_only", False)
                    for mount in service["volumes"]
                )
                for service in (bot, backup)
            ),
        )
        check(
            "settings 금지어 bind 제거",
            all(
                not str(mount.get("source", "")).endswith(
                    "settings/forbidden_words.json"
                )
                for service in (bot, backup)
                for mount in service["volumes"]
            ),
        )
        check(
            "backup 설정 마운트는 읽기 전용",
            any(
                str(mount.get("source", "")).endswith("settings")
                and mount.get("target") == "/app/settings"
                and mount.get("read_only", False)
                for mount in backup["volumes"]
            ),
        )
        check(
            "Compose 로그 크기 제한",
            all(
                service.get("logging", {}).get("driver") == "json-file"
                and service.get("logging", {}).get("options", {}).get("max-size")
                == "10m"
                for service in (bot, backup)
            ),
        )
        check(
            "Compose 로그 파일 수 제한",
            all(
                service.get("logging", {}).get("options", {}).get("max-file")
                == "5"
                for service in (bot, backup)
            ),
        )
    check(
        "launchd 백업은 env 주기 loop 사용",
        backup_plist["ProgramArguments"]
        == [
            bot_plist["ProgramArguments"][0],
            "-m",
            "module.backup",
            "loop",
        ]
        and "StartInterval" not in backup_plist
        and backup_plist.get("KeepAlive") is True,
    )
    check(
        "newsyslog는 두 LaunchAgent 로그만 관리",
        len(newsyslog_entries) == 4
        and all(len(entry) == 9 for entry in newsyslog_entries)
        and {entry[0] for entry in newsyslog_entries}
        == {
            bot_plist["StandardOutPath"],
            bot_plist["StandardErrorPath"],
            backup_plist["StandardOutPath"],
            backup_plist["StandardErrorPath"],
        },
    )
    check(
        "newsyslog template owner/group placeholder 명시",
        all(entry[1] == "__USER__:__GROUP__" for entry in newsyslog_entries),
    )
    working_directory = pathlib.Path(bot_plist["WorkingDirectory"])
    expected_pid_by_log = {
        bot_plist["StandardOutPath"]: str(
            working_directory / "runtime/data/.bot.pid"
        ),
        bot_plist["StandardErrorPath"]: str(
            working_directory / "runtime/data/.bot.pid"
        ),
        backup_plist["StandardOutPath"]: str(
            working_directory / "runtime/backups/.backup.pid"
        ),
        backup_plist["StandardErrorPath"]: str(
            working_directory / "runtime/backups/.backup.pid"
        ),
    }
    check(
        "newsyslog 네 항목은 올바른 PID에 SIGHUP 전달",
        all(
            len(entry) == 9
            and entry[7] == expected_pid_by_log.get(entry[0])
            and entry[8] == "1"
            for entry in newsyslog_entries
        ),
    )


def test_macos_templates_render_portably():
    import plistlib
    from deploy.macos.render_templates import render_templates

    template_dir = PROJECT_ROOT / "deploy/macos"
    template_texts = [
        (template_dir / name).read_text(encoding="utf-8")
        for name in (
            "com.discordbot.hsr.plist.example",
            "com.discordbot.hsr-backup.plist.example",
            "com.discordbot.hsr.newsyslog.conf.example",
        )
    ]
    check(
        "macOS templates have no author-specific values",
        all(
            "/Users/strawberrydreams" not in text
            and "strawberrydreams:staff" not in text
            for text in template_texts
        ),
    )

    project_root = pathlib.Path("/tmp/portable&<clone>#/discordbot-hsr")
    with tempfile.TemporaryDirectory() as directory:
        output_dir = pathlib.Path(directory)
        render_templates(project_root, output_dir, "portable-user", "portable-group")
        bot_plist = plistlib.loads((output_dir / "com.discordbot.hsr.plist").read_bytes())
        backup_plist = plistlib.loads(
            (output_dir / "com.discordbot.hsr-backup.plist").read_bytes()
        )
        newsyslog = (output_dir / "com.discordbot.hsr.conf").read_text(encoding="utf-8")

    check(
        "rendered plist paths use clone root",
        all(
            project_root.as_posix() in value
            for plist in (bot_plist, backup_plist)
            for value in (
                plist["ProgramArguments"][0],
                plist["WorkingDirectory"],
                plist["StandardOutPath"],
                plist["StandardErrorPath"],
            )
        ),
    )
    check(
        "rendered newsyslog uses clone user and group",
        "/tmp/portable&<clone>\\#/discordbot-hsr" in newsyslog
        and "portable-user:portable-group" in newsyslog
        and not any(
            placeholder in newsyslog
            for placeholder in ("__PROJECT_ROOT__", "__USER__", "__GROUP__")
        ),
    )
    with tempfile.TemporaryDirectory() as directory:
        output_dir = pathlib.Path(directory)
        try:
            render_templates(
                pathlib.Path("/tmp/portable clone/discordbot-hsr"),
                output_dir,
                "portable-user",
                "portable-group",
            )
            rejected = False
        except RuntimeError:
            rejected = True
        check(
            "newsyslog-incompatible whitespace path rejected before output",
            rejected and not any(output_dir.iterdir()),
        )

    collision_cases = (
        (pathlib.Path("/tmp/__USER__/discordbot-hsr"), "portable-user", "portable-group"),
        (pathlib.Path("/tmp/portable/discordbot-hsr"), "portable__GROUP__", "portable-group"),
        (pathlib.Path("/tmp/portable/discordbot-hsr"), "portable-user", "__PROJECT_ROOT__"),
    )
    for index, (root, user, group) in enumerate(collision_cases):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = pathlib.Path(directory)
            try:
                render_templates(root, output_dir, user, group)
                rejected = False
            except RuntimeError:
                rejected = True
            check(
                f"reserved renderer token input {index + 1} rejected before output",
                rejected and not any(output_dir.iterdir()),
            )


def test_deployment_contracts_skip_only_compose_when_cli_missing():
    output = StringIO()
    error = None
    with patch.object(shutil, "which", return_value=None), redirect_stdout(output):
        try:
            test_deployment_contracts()
        except (OSError, subprocess.SubprocessError) as exc:
            error = exc

    rendered = output.getvalue()
    check("Compose CLI 부재 시 deployment suite 계속 실행", error is None)
    check(
        "Compose CLI 부재를 명시적 SKIP으로 출력",
        "SKIP: rendered Compose deployment checks" in rendered,
    )
    check(
        "Compose CLI 부재에도 plist/newsyslog 검증 유지",
        "launchd 백업은 env 주기 loop 사용" in rendered
        and "newsyslog 네 항목은 올바른 PID에 SIGHUP 전달" in rendered,
    )


def test_forbidden_words_degrade_gracefully():
    import module.config as config
    from module.forbiddenfilter_cog import load_forbidden_words

    with tempfile.TemporaryDirectory() as directory:
        settings_dir = pathlib.Path(directory)
        with patch.object(config, "SETTINGS_DIR", settings_dir):
            check("금지어 파일 누락 시 필터 비활성", load_forbidden_words() == [])
            (settings_dir / "forbidden_words.json").write_text("{}", encoding="utf-8")
            check("금지어 JSON 구조 오류 시 필터 비활성", load_forbidden_words() == [])


def test_forbidden_words_load_logs_to_stdout():
    import module.forbiddenfilter_cog as forbiddenfilter_cog

    import module.config as config

    with tempfile.TemporaryDirectory() as directory:
        pathlib.Path(directory, "forbidden_words.json").write_text(
            '["금지어"]', encoding="utf-8"
        )
        output = StringIO()
        with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)), redirect_stdout(output):
            forbiddenfilter_cog.ForbiddenFilterCog(bot=None)

    check(
        "금지어 로드 로그가 stdout에 기록",
        output.getvalue().strip() == "📥 금지어 1개 로드",
    )


def test_startup_syncs_commands_globally():
    """공개 배포 봇은 전역 sync다. 길드 한정 sync는 설치될 서버를 미리 알아야 한다."""
    import module.main as main

    events = []
    data_dir = _TMP_DIR / "global-sync-data"
    data_dir.mkdir(exist_ok=True)

    class FakeTree:
        async def sync(self, *, guild=None):
            events.append("sync:global" if guild is None else f"sync:guild:{guild}")

    class FakeBot:
        tree = FakeTree()

        def add_view(self, view):
            events.append("view:setup")

        async def add_cog(self, cog):
            events.append("cog:guildsettings")

        async def load_extension(self, extension):
            events.append(f"load:{extension}")
            if extension == "module.guildsettings_cog":
                from module.guildsettings_cog import setup

                await setup(self)

    def verify(*, existing_only=False, allow_legacy=False):
        events.append("verify:pre" if existing_only and allow_legacy else "verify:post")

    with patch.object(main, "DATA_DIR", data_dir), \
         patch.object(main, "_verify_databases", side_effect=verify):
        asyncio.run(main.MyBot.setup_hook(FakeBot()))

    check("전역으로만 sync", [e for e in events if e.startswith("sync")] == ["sync:global"],
          f"({events})")
    check("sync는 모든 Cog 로드 후", events[-1] == "sync:global", f"({events})")
    check(
        "persistent SetupView는 전역 sync 전에 등록",
        events.index("view:setup") < events.index("sync:global"),
        f"({events})",
    )
    check(
        "DB 사전 검증·마이그레이션 뒤 사후 검증 후 sync",
        events.index("verify:pre") < events.index("load:module.guildsettings_cog")
        < events.index("verify:post")
        < events.index("sync:global"),
        f"({events})",
    )
    check(
        "길드 설정 Cog가 로드 목록에 포함",
        "load:module.guildsettings_cog" in events,
        f"({events})",
    )
    check(
        "길드 고정 잔재 없음",
        "copy_global_to" not in inspect.getsource(main.MyBot.setup_hook)
        and "interaction_check" not in inspect.getsource(main.MyBot.setup_hook),
    )


def test_startup_preverification_failure_stops_cogs_and_sync():
    import module.main as main

    events = []

    class FakeBot:
        class tree:
            @staticmethod
            async def sync():
                events.append("sync")

        async def load_extension(self, extension):
            events.append(f"load:{extension}")

    def fail_verify(path, tables, **kwargs):
        events.append(f"verify:{path.name}")
        raise RuntimeError("missing production database")

    with patch.object(pathlib.Path, "exists", return_value=True), \
         patch.object(main, "verify_database", side_effect=fail_verify):
        try:
            asyncio.run(main.MyBot.setup_hook(FakeBot()))
            check("사전 DB 검증 실패 전파", False)
        except RuntimeError:
            check("사전 DB 검증 실패 전파", True)
    check("사전 DB 검증 실패 시 Cog와 sync 미실행", events == ["verify:attendance_data.db"], f"({events})")


def test_startup_migrates_legacy_attendance_before_strict_verification():
    import module.main as main

    data_dir = _TMP_DIR / "legacy-startup-data"
    attendance_path = data_dir / "attendance_data.db"
    _create_legacy_attendance_db(attendance_path)
    SQLitePartyRepository(data_dir / "party_data.db")
    SQLiteGuildSettingsRepository(data_dir / "guild_settings.db")
    events = []

    class FakeBot:
        class tree:
            @staticmethod
            async def sync():
                events.append("sync")

        async def load_extension(self, extension):
            events.append(f"load:{extension}")
            if extension == "module.attendance_cog":
                SQLiteAttendanceRepository(attendance_path)
                events.append("migrate:attendance")

    with patch.object(main, "DATA_DIR", data_dir):
        try:
            asyncio.run(main.MyBot.setup_hook(FakeBot()))
            started = True
        except RuntimeError:
            started = False

    with closing(sqlite3.connect(attendance_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        points = conn.execute(
            "SELECT points FROM users WHERE guild_id = 7 AND user_id = 8"
        ).fetchone()
    check("legacy attendance startup reaches repository migration", started)
    check(
        "startup migration precedes sync and preserves data",
        version == 2
        and "ai_usage" in tables
        and points == (9000,)
        and "migrate:attendance" in events
        and events.index("migrate:attendance") < events.index("sync"),
        f"(version={version}, events={events})",
    )


def test_startup_cog_failure_stops_postverification_and_sync():
    import module.backup as backup
    import module.main as main

    events = []

    class FakeBot:
        class tree:
            @staticmethod
            async def sync():
                events.append("sync")

        async def load_extension(self, extension):
            events.append(f"load:{extension}")
            if extension == main.EXTENSIONS[1][0]:
                raise RuntimeError("broken cog")

    def verify(path, tables, **kwargs):
        events.append(f"verify:{path.name}")
        return {}

    with patch.object(pathlib.Path, "exists", return_value=True), \
         patch.object(main, "verify_database", side_effect=verify):
        try:
            asyncio.run(main.MyBot.setup_hook(FakeBot()))
            check("Cog 로드 실패 전파", False)
        except RuntimeError:
            check("Cog 로드 실패 전파", True)
    check(
        "Cog 로드 실패 시 사후 검증과 sync 미실행",
        events == [
            *(f"verify:{name}" for name in backup.DATABASES),
            f"load:{main.EXTENSIONS[0][0]}",
            f"load:{main.EXTENSIONS[1][0]}",
        ],
        f"({events})",
    )


def test_instance_lock_rejects_second_holder():
    import module.main as main

    if not hasattr(main, "acquire_instance_lock"):
        check("인스턴스 잠금 API 제공", False)
        return

    lock_path = _TMP_DIR / ".bot.lock"
    first = main.acquire_instance_lock(lock_path)
    try:
        try:
            main.acquire_instance_lock(lock_path)
            rejected = False
        except RuntimeError:
            rejected = True
        check("두 번째 봇 인스턴스 거부", rejected)
    finally:
        first.close()

    reacquired = main.acquire_instance_lock(lock_path)
    reacquired.close()
    check("첫 번째 잠금 해제 후 재획득", True)


def test_instance_lock_closes_failed_handle():
    import module.main as main

    if not hasattr(main, "acquire_instance_lock"):
        check("잠금 실패 파일 핸들 정리 API 제공", False)
        return

    lock = (_TMP_DIR / ".failed-bot.lock").open("a+b")
    with patch.object(pathlib.Path, "open", return_value=lock), \
         patch.object(main.fcntl, "flock", side_effect=BlockingIOError):
        try:
            main.acquire_instance_lock(_TMP_DIR / ".failed-bot.lock")
            rejected = False
        except RuntimeError:
            rejected = True

    check("잠금 경합 오류 전환", rejected)
    check("잠금 실패 파일 핸들 닫힘", lock.closed)


def test_main_holds_instance_lock_while_bot_runs():
    import module.main as main

    if not hasattr(main, "acquire_instance_lock"):
        check("main 인스턴스 잠금 API 제공", False)
        return

    events = []
    data_dir = _TMP_DIR / "main-pid-data"
    backup_dir = _TMP_DIR / "main-pid-backups"
    data_dir.mkdir(exist_ok=True)
    backup_dir.mkdir(exist_ok=True)
    pid_path = data_dir / ".bot.pid"
    pid_path.unlink(missing_ok=True)

    class FakeLock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, exc_type, exc, traceback):
            events.append("unlock")

    class FakeBot:
        def run(self, token):
            value = pid_path.read_text(encoding="ascii").strip() if pid_path.exists() else "missing"
            events.append(f"pid:{value}")
            events.append("run")

    def acquire(path):
        events.append(f"acquire:{path.name}")
        return FakeLock()

    with patch.object(main, "validate_config"), \
         patch.object(main, "DATA_DIR", data_dir), \
         patch.object(main, "BACKUP_DIR", backup_dir), \
         patch.object(main, "acquire_instance_lock", side_effect=acquire), \
         patch.object(main, "MyBot", FakeBot), \
         patch.object(sys, "platform", "darwin"):
        main.main()

    check(
        "봇 실행 수명 동안 lock과 현재 PID 유지",
        events
        == [
            "acquire:.bot.lock",
            "lock",
            f"pid:{os.getpid()}",
            "run",
            "unlock",
        ],
        f"({events})",
    )
    check("봇 정상 종료 시 PID 파일 정리", not pid_path.exists())


def test_importing_main_does_not_construct_bot():
    sys.modules.pop("module.main", None)
    from discord.ext import commands

    with patch.object(commands.Bot, "__init__", side_effect=AssertionError("bot constructed")):
        try:
            importlib.import_module("module.main")
            check("main import는 Bot을 생성하지 않음", True)
        except AssertionError:
            check("main import는 Bot을 생성하지 않음", False)


def test_bot_disables_all_mentions():
    import module.main as main
    from discord.ext import commands

    with patch.object(commands.Bot, "__init__", return_value=None) as init:
        main.MyBot()

    allowed = init.call_args.kwargs["allowed_mentions"]
    check("봇 전역 멘션 차단", allowed.everyone is False and allowed.roles is False and allowed.users is False)


def test_sqlite_busy_timeout():
    print("\n[0] SQLite 연결 정책")
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "t.db"
        with closing(database._connect(path)) as conn:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        check("busy timeout 30초 적용", timeout_ms == 30_000, f"({timeout_ms}ms)")

    source = pathlib.Path(inspect.getsourcefile(database)).read_text(encoding="utf-8")
    direct = source.count("sqlite3.connect(")
    check("모든 연결이 헬퍼를 경유", direct == 1, f"직접 호출 {direct}건")


def test_backup_reads_wal_without_writer():
    import module.backup as backup

    with tempfile.TemporaryDirectory() as directory:
        data_dir = pathlib.Path(directory) / "data"
        data_dir.mkdir()
        source = data_dir / "attendance_data.db"
        repo = SQLiteAttendanceRepository(source)
        repo = _bind(repo)
        repo.add_points(1, 10)
        del repo  # 쓰기 연결 없음 = 봇 정지 상태
        gc.collect()

        with closing(sqlite3.connect(source)) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        check("WAL 모드로 저장됨", mode == "wal", f"({mode})")

        target = pathlib.Path(directory) / "copy.db"
        backup._backup_one(source, target)
        with closing(sqlite3.connect(target)) as conn:
            points = conn.execute("SELECT points FROM users WHERE user_id = 1").fetchone()
        check("쓰기 프로세스 없이도 백업 가능", points == (10,), f"({points})")


def test_luckybox_removed():
    print("\n[0] 럭키박스 제거")
    import module.attendance_cog as attendance_cog

    check(
        "play_luckybox 인터페이스 제거",
        not hasattr(database.AttendanceRepository, "play_luckybox"),
    )
    names = {c.name for c in AttendanceCog(bot=None).get_app_commands()}
    check("럭키박스 명령 제거", "럭키박스" not in names, f"({sorted(names)})")

    repo = SQLiteAttendanceRepository(_TMP_DIR / "luckybox_columns.db")
    repo = _bind(repo)
    with closing(sqlite3.connect(repo.db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    check(
        "럭키박스 컬럼 완전 제거",
        not {"luckybox_count", "last_luckybox_date"} & columns,
        f"({sorted(columns)})",
    )
    check(
        "출석 외 통화 발행 없음",
        "add_points" not in inspect.getsource(attendance_cog.AttendanceCog._attend.callback),
    )


def test_point_ledger():
    print("\n[0] 포인트 원장")
    with tempfile.TemporaryDirectory() as directory:
        repo = SQLiteAttendanceRepository(pathlib.Path(directory) / "a.db")
        repo = _bind(repo)
        repo.add_points(1, 500, reason="attendance")
        check(
            "차감 실패는 원장에 남기지 않음",
            repo.deduct_points(1, 9_999, reason="image") is False,
        )
        check("차감 성공", repo.deduct_points(1, 200, reason="image") is True)
        repo.add_points(1, 200, reason="image_refund")

        entries = repo.get_ledger(1, limit=10)
        check("모든 성공 이동이 기록됨", len(entries) == 3, f"({entries})")
        check(
            "원장 합계가 잔액과 일치",
            sum(delta for delta, _, _ in entries) == repo.get_points(1),
        )
        check(
            "실패한 차감은 기록되지 않음",
            all(reason != "image" or delta == -200 for delta, reason, _ in entries),
        )
        check(
            "출석 지급도 원장에 기록",
            repo.claim_attendance(2, 7_000, "2026-07-30") == 7_000
            and repo.get_ledger(2) == [(7_000, "attendance", repo.get_ledger(2)[0][2])],
        )
        check(
            "중복 출석은 원장에 기록되지 않음",
            repo.claim_attendance(2, 7_000, "2026-07-30") is None
            and len(repo.get_ledger(2)) == 1,
        )


def test_guild_isolation():
    print("\n[0] 길드 격리")
    import module.database as database
    import module.forbiddenfilter_cog as forbiddenfilter_cog
    import module.playwith_cog as playwith_cog

    data_dir = _TMP_DIR / "guild-isolation"
    data_dir.mkdir(exist_ok=True)
    points = database.SQLiteAttendanceRepository(data_dir / "a.db")
    parties = database.SQLitePartyRepository(data_dir / "p.db")
    settings = database.SQLiteGuildSettingsRepository(data_dir / "s.db")

    A, B, USER = 1001, 1002, 7
    # 같은 사람이 서버마다 별도 잔액을 갖는다. 경계는 코드가 아니라 스키마가 만든다.
    points.add_points(A, USER, 500, "t")
    points.add_points(B, USER, 900, "t")
    check("포인트는 서버별로 분리", (points.get_points(A, USER), points.get_points(B, USER)) == (500, 900))
    check("차감은 해당 서버만", points.deduct_points(A, USER, 500, "t") and points.get_points(B, USER) == 900)
    check("랭킹은 자기 서버만", points.get_top_rankings(B) == [(USER, 900)])

    # 출석은 서버마다 따로 받는다.
    check("A서버 출석", points.claim_attendance(A, USER, 10, "2026-07-31") is not None)
    check("A서버 재출석 거부", points.claim_attendance(A, USER, 10, "2026-07-31") is None)
    check("B서버는 여전히 출석 가능", points.claim_attendance(B, USER, 10, "2026-07-31") is not None)

    # 금지어 카운트도 서버별.
    points.increment_forbidden_count(A, USER)
    check("금지어 카운트 분리", (points.get_forbidden_count(A, USER), points.get_forbidden_count(B, USER)) == (1, 0))

    # 파티: 같은 게임을 서버마다 독립적으로 연다.
    check("같은 게임을 두 서버가 각각 생성", parties.create_party(A, "PUBG", 1) and parties.create_party(B, "PUBG", 1))
    check("역할 중복은 같은 서버에서만 거부", [
        parties.add_participant(A, "PUBG", 1, "탑", 4),
        parties.add_participant(A, "PUBG", 2, "탑", 4),
        parties.add_participant(B, "PUBG", 2, "탑", 4),
    ] == [True, False, True])
    check("참가 조회는 서버별", parties.get_user_party(A, 1) == "PUBG" and parties.get_user_party(B, 1) is None)

    # 설정도 서버별.
    settings.set_party_channel(A, 333)
    check("설정 분리", settings.get_party_channel(A) == 333 and settings.get_party_channel(B) is None)

    # 봇이 서버에서 제거되면 그 서버 것만 지운다.
    for repo in (points, parties, settings):
        repo.delete_guild(A)
    check("제거된 서버 데이터 삭제", points.get_points(A, USER) == 0 and parties.get_party(A, "PUBG") is None
          and settings.get_party_channel(A) is None)
    check("다른 서버는 보존", points.get_points(B, USER) == 910 and parties.get_party(B, "PUBG") is not None)

    # 스키마가 경계를 강제하는지 — 모든 기본키에 guild_id가 있어야 한다.
    import sqlite3
    with sqlite3.connect(data_dir / "a.db") as conn:
        pk = [r[1] for r in conn.execute("PRAGMA table_info(users)") if r[5]]
    check("users 기본키에 guild_id 포함", "guild_id" in pk, f"({pk})")

    check(
        "DM은 금지어 집계에서 제외",
        "message.guild is None"
        in inspect.getsource(forbiddenfilter_cog.ForbiddenFilterCog._inspect),
    )
    check(
        "파티 패널은 길드 밖 상호작용을 거부",
        "guild_id is None"
        in inspect.getsource(playwith_cog.PlayWithCog._reject_invalid_interaction),
    )


def test_temp_image_lifecycle():
    print("\n[0] 임시 이미지 경로와 수명")
    import module.config as config
    import module.hyacine_image_cog as hyacine_image_cog

    cog = object.__new__(hyacine_image_cog.HyacineImageCog)
    cog.temp_dir = config.DATA_DIR / "temp_images"
    cog.temp_dir.mkdir(parents=True, exist_ok=True)
    check("임시 경로는 DATA_DIR 아래", cog.temp_dir.is_relative_to(config.DATA_DIR))

    stale = cog.temp_dir / "stale.png"
    fresh = cog.temp_dir / "fresh.png"
    stale.write_bytes(b"png")
    fresh.write_bytes(b"png")
    old = time.time() - hyacine_image_cog.TEMP_IMAGE_TTL_SECONDS - 60
    os.utime(stale, (old, old))

    cog._sweep_stale_images()

    check("시작 시 오래된 임시 파일 정리", not stale.exists())
    check("최근 파일은 보존", fresh.exists())
    fresh.unlink()


def test_schema_initialization() -> SQLiteAttendanceRepository:
    print("\n[1] SQLite 스키마 초기화")
    db_path = _TMP_DIR / "attendance_schema.db"

    repo = SQLiteAttendanceRepository(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        pk = [row[1] for row in conn.execute("PRAGMA table_info(users)") if row[5]]
        ai_cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_usage)")}
        ai_pk = [row[1] for row in conn.execute("PRAGMA table_info(ai_usage)") if row[5]]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    check("users 컬럼 구성", cols == {"guild_id", "user_id", "points",
                                     "last_attendance_date", "forbidden_count"}, f"({cols})")
    check("복합 기본키 (guild_id, user_id)", pk == ["guild_id", "user_id"], f"({pk})")
    check("원장 테이블 생성", "point_ledger" in tables)
    check("AI 사용량 테이블 생성", "ai_usage" in tables)
    check("AI 사용량은 guild_id 없이 전역", ai_cols == {"user_id", "usage_date", "command", "count"})
    check("AI 사용량 복합 기본키", ai_pk == ["user_id", "usage_date", "command"])
    check("폐기된 luckybox 컬럼 없음", not {"luckybox_count", "last_luckybox_date"} & cols)

    # 중복 실행해도 에러가 없어야 함 (멱등성)
    SQLiteAttendanceRepository(db_path)
    check("스키마 재생성 시 에러 없음 (멱등성)", True)
    return _bind(repo)


def _user_version(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_schema_versions():
    print("\n[1] SQLite 스키마 버전")
    attendance_path = _TMP_DIR / "attendance_version.db"
    party_path = _TMP_DIR / "party_version.db"
    settings_path = _TMP_DIR / "settings_version.db"
    SQLiteAttendanceRepository(attendance_path)
    SQLitePartyRepository(party_path)
    SQLiteGuildSettingsRepository(settings_path)

    check("attendance 스키마 버전", _user_version(attendance_path) == 2)
    check("party 스키마 버전", _user_version(party_path) == 2)
    check("settings 스키마 버전", _user_version(settings_path) == 2)

    for label, repository in (
        ("attendance", SQLiteAttendanceRepository),
        ("party", SQLitePartyRepository),
        ("settings", SQLiteGuildSettingsRepository),
    ):
        path = _TMP_DIR / f"future_{label}.db"
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA user_version = 999")
        try:
            repository(path)
        except RuntimeError:
            rejected = True
        else:
            rejected = False
        check(f"미래 {label} DB 버전 거부", rejected)

    with sqlite3.connect(attendance_path) as conn:
        conn.execute("PRAGMA user_version = 0")
        conn.execute("INSERT INTO users VALUES (1, 2, 3, NULL, 0)")
    SQLiteAttendanceRepository(attendance_path)
    check(
        "무버전 attendance 데이터 보존",
        SQLiteAttendanceRepository(attendance_path).get_points(1, 2) == 3,
    )

    version_one_path = _TMP_DIR / "attendance_version_one.db"
    with sqlite3.connect(version_one_path) as conn:
        conn.executescript("""
            CREATE TABLE users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                last_attendance_date TEXT,
                forbidden_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE point_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            INSERT INTO users VALUES (7, 8, 9000, NULL, 0);
            PRAGMA user_version = 1;
        """)
    migrated = SQLiteAttendanceRepository(version_one_path)
    check("attendance v1에서 v2로 마이그레이션", _user_version(version_one_path) == 2)
    check("attendance v1 포인트 보존", migrated.get_points(7, 8) == 9_000)

    with sqlite3.connect(party_path) as conn:
        conn.execute("PRAGMA user_version = 0")
        conn.execute(
            "INSERT INTO parties (guild_id, game, created_at) VALUES (1, 'LOL', 3)"
        )
    SQLitePartyRepository(party_path)
    check(
        "무버전 party 데이터 보존",
        SQLitePartyRepository(party_path).get_party(1, "LOL") == (3,),
    )

    for legacy_version in (0, 1):
        legacy_party_path = _TMP_DIR / f"party_v{legacy_version}_to_v2.db"
        with sqlite3.connect(legacy_party_path) as conn:
            conn.executescript(f"""
                CREATE TABLE parties (
                    guild_id INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, game)
                );
                CREATE TABLE participants (
                    guild_id INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    role TEXT,
                    PRIMARY KEY (guild_id, game, user_id)
                );
                INSERT INTO parties VALUES (1, 'A', 10), (1, 'B', 20);
                INSERT INTO participants VALUES
                    (1, 'B', 7, 'B-role'),
                    (1, 'A', 7, 'A-role'),
                    (1, 'A', 8, NULL);
                PRAGMA user_version = {legacy_version};
            """)
        party_v2 = SQLitePartyRepository(legacy_party_path)
        check(
            f"party v{legacy_version}에서 v2로 마이그레이션",
            _user_version(legacy_party_path) == 2,
        )
        check(
            f"party v{legacy_version} 중복 참가를 game 이름 순으로 정리",
            party_v2.get_user_party(1, 7) == "A"
            and party_v2.get_participants(1, "B") == {},
        )
        check(
            f"party v{legacy_version} 방장 결정",
            party_v2.get_party_host(1, "A") == 7,
        )

    for legacy_version in (0, 1):
        legacy_path = _TMP_DIR / f"settings_v{legacy_version}_to_v2.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, recruit_channel_id INTEGER, event_channel_id INTEGER)"
            )
            conn.execute("INSERT INTO guild_settings VALUES (7, 700, 701)")
            conn.execute(f"PRAGMA user_version = {legacy_version}")
        SQLiteGuildSettingsRepository(legacy_path)
        with sqlite3.connect(legacy_path) as conn:
            row = conn.execute(
                "SELECT party_channel_id, music_channel_id, music_panel_msg_id, allow_host_announce FROM guild_settings WHERE guild_id = 7"
            ).fetchone()
            tables = {
                item[0]
                for item in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            guild_columns = tuple(
                row[1] for row in conn.execute("PRAGMA table_info(guild_settings)")
            )
            panel_info = list(conn.execute("PRAGMA table_info(party_panels)"))
            panel_columns = tuple(row[1] for row in panel_info)
            panel_primary_key = tuple(
                row[1] for row in sorted(panel_info, key=lambda row: row[5]) if row[5]
            )
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        check(f"settings v{legacy_version} recruit 보존", row == (700, None, None, 0))
        check(f"settings v{legacy_version} party_panels 생성", "party_panels" in tables)
        check(
            f"settings v{legacy_version} 정확한 패널 스키마",
            guild_columns == (
                "guild_id",
                "party_channel_id",
                "music_channel_id",
                "music_panel_msg_id",
                "allow_host_announce",
            )
            and panel_columns == ("guild_id", "game", "message_id")
            and panel_primary_key == ("guild_id", "game"),
        )
        check(f"settings v{legacy_version} 버전", version == 2)

    malformed_schemas = {
        "extra guild_settings column": """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                music_channel_id INTEGER,
                music_panel_msg_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0,
                obsolete INTEGER
            )
        """,
        "wrong party_panels primary key": """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                music_channel_id INTEGER,
                music_panel_msg_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE party_panels (
                guild_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, message_id)
            )
        """,
    }
    malformed_rejected = []
    for label, schema in malformed_schemas.items():
        malformed_path = _TMP_DIR / f"settings_{label.replace(' ', '_')}.db"
        with sqlite3.connect(malformed_path) as conn:
            conn.executescript(schema)
        try:
            SQLiteGuildSettingsRepository(malformed_path)
        except RuntimeError:
            malformed_rejected.append(True)
        else:
            malformed_rejected.append(False)
    check("잘못된 현재 settings 스키마 거부", all(malformed_rejected))

    settings = SQLiteGuildSettingsRepository(_TMP_DIR / "settings_repository.db")
    settings.set_party_panel(7, "LOL", 70)
    settings.set_party_panel(7, "LOL", 71)
    settings.set_party_panel(7, "PUBG", 72)
    panels_after_upsert = settings.get_party_panels(7)
    settings.delete_party_panel(7, "LOL")
    settings.set_party_channel(7, 700)
    settings.set_music_channel(7, 701)
    settings.set_music_panel_msg(7, 702)
    settings.set_allow_host_announce(7, True)
    settings.set_allow_host_announce(8, True)
    settings.clear_channel(7, 701)
    check("party panel upsert/list/delete", panels_after_upsert == {"LOL": 71, "PUBG": 72} and settings.get_party_panels(7) == {"PUBG": 72})
    check("삭제된 음악 채널은 패널 메시지도 해제", settings.get_party_channel(7) == 700 and settings.get_music_channel(7) is None and settings.get_music_panel_msg(7) is None)
    settings.set_music_channel(7, 701)
    settings.set_music_panel_msg(7, 702)
    settings.clear_channel(7, 700)
    check("삭제된 파티 채널은 음악 설정을 보존하고 해제", settings.get_party_channel(7) is None and settings.get_music_channel(7) == 701 and settings.get_music_panel_msg(7) == 702)
    check("공지 허용 길드 목록", settings.get_allow_host_announce(7) and settings.list_announcement_guild_ids() == [7, 8])
    settings.delete_guild(7)
    check("길드 삭제는 설정과 party panel 정리", settings.get_party_panels(7) == {} and settings.get_party_channel(7) is None)


def test_deduct_points_atomicity(repo: SQLiteAttendanceRepository):
    print("\n[2] deduct_points 원자성")
    user = 100
    repo.add_points(user, 10_000)

    check("잔액 부족 시 차감 거부", repo.deduct_points(user, 99_999) is False)
    check("잔액 변동 없음", repo.get_points(user) == 10_000)
    check("정상 차감 성공", repo.deduct_points(user, 3_000) is True)
    check("차감 후 잔액 일치", repo.get_points(user) == 7_000)

    # 동시 차감: 7,000 P 보유, 20개 스레드가 동시에 1,000 P씩 차감 시도
    # -> 정확히 7번만 성공해야 하고 잔액은 0이어야 함
    results = []
    lock = threading.Lock()

    def worker():
        ok = repo.deduct_points(user, 1_000)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    successes = sum(results)
    final = repo.get_points(user)
    check("동시 차감: 성공 횟수가 잔액과 정확히 일치 (7회)", successes == 7, f"(성공 {successes}회)")
    check("동시 차감: 최종 잔액 0, 음수 아님", final == 0, f"(잔액 {final})")


def test_ai_usage_atomicity():
    print("\n[2] AI 일일 사용량 원자성")
    repo = SQLiteAttendanceRepository(_TMP_DIR / "ai_usage_atomicity.db")
    user_id = 200
    usage_date = "2026-08-04"
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        try:
            result = repo.consume_ai_usage(user_id, usage_date, "light", 3)
            with lock:
                results.append(result)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check("AI 사용량 20개 호출 모두 정상 완료", len(results) == 20 and not errors)
    check("limit=3은 정확히 3번만 성공", sum(result is not None for result in results) == 3)
    check("light 사용량은 3", repo.get_ai_usage(user_id, usage_date, "light") == 3)
    check("deep은 light와 분리", repo.consume_ai_usage(user_id, usage_date, "deep", 3) == 1)
    check("다음 날은 새 한도", repo.consume_ai_usage(user_id, "2026-08-05", "light", 3) == 1)
    check("예약 반환 3회 성공", all(repo.release_ai_usage(user_id, usage_date, "light") for _ in range(3)))
    check("0에서 추가 반환 거부", repo.release_ai_usage(user_id, usage_date, "light") is False)
    check("사용량은 0 아래로 내려가지 않음", repo.get_ai_usage(user_id, usage_date, "light") == 0)


def test_attendance_atomicity(repo: SQLiteAttendanceRepository):
    print("\n[3] claim_attendance 원자성")
    user_id = 150
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        try:
            result = repo.claim_attendance(user_id, 10_000, "2026-07-29")
            with lock:
                results.append(result)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check(
        "동시 출석 20개 호출 모두 정상 완료",
        len(results) == len(threads) and not errors,
        f"(완료 {len(results)}개, 예외 {errors})",
    )
    check("동시 출석은 정확히 한 번 성공", sum(result is not None for result in results) == 1)
    check("동시 출석 포인트는 한 번만 지급", repo.get_points(user_id) == 10_000)

    existing_user_id = 151
    repo.add_points(existing_user_id, 5_000)
    new_points = repo.claim_attendance(existing_user_id, 10_000, "2026-07-29")
    check(
        "기존 유저 출석은 reward만큼 증가",
        new_points == 15_000 and repo.get_points(existing_user_id) == 15_000,
    )

    duplicate = repo.claim_attendance(existing_user_id, 99_999, "2026-07-29")
    check("같은 날짜 순차 중복은 None", duplicate is None)
    check("같은 날짜 순차 중복은 잔액 불변", repo.get_points(existing_user_id) == 15_000)


def test_party_repository():
    print("\n[4] PartyRepository CRUD")
    repo = SQLitePartyRepository(_TMP_DIR / "party_test.db")
    repo = _bind(repo)
    now = 2_000_000_000

    check("없는 파티 조회 시 None", repo.get_party("LOL") is None)
    check("없는 파티 참가 거부", repo.add_participant("missing", 99) is False)
    check("거부된 참가자는 고아 행을 남기지 않음", repo.get_user_party(99) is None)

    check("첫 생성은 True", repo.create_party("LOL", now) is True)
    check("파티 시각을 epoch 정수로 저장", repo.get_party("LOL") == (now,))

    check("중복 생성은 False", repo.create_party("LOL", now + 1) is False)
    check("중복 생성이 시각을 덮어쓰지 않음", repo.get_party("LOL") == (now,))

    repo.add_participant("LOL", 1, "탑")
    repo.add_participant("LOL", 2, None)
    check("참가자 2명 등록", repo.get_participants("LOL") == {1: "탑", 2: None})
    check("유저의 참가 파티 조회", repo.get_user_party(1) == "LOL")
    check("미참가 유저는 None", repo.get_user_party(999) is None)

    repo.add_participant("LOL", 1, "정글")  # 역할 변경 (REPLACE)
    check("역할 변경 반영", repo.get_participants("LOL")[1] == "정글")

    repo.remove_participant("LOL", 2)
    check("참가자 제거", 2 not in repo.get_participants("LOL"))

    repo.delete_party("LOL")
    check("파티 삭제", repo.get_party("LOL") is None)
    check("파티 삭제 시 참가자도 정리됨", repo.get_user_party(1) is None)

    # 만료 정리: 25시간 전 파티는 삭제, 방금 만든 파티는 유지
    old_time = now - 25 * 60 * 60
    repo.create_party("PUBG", old_time)
    repo.add_participant("PUBG", 3)
    repo.create_party("Overwatch", now)
    cutoff = now - 24 * 60 * 60

    expired = repo.delete_expired_parties(cutoff)
    check("만료 파티만 삭제됨", expired == [(_TEST_GUILD, "PUBG")], f"(삭제 목록 {expired})")
    check("만료 파티 참가자도 정리됨", repo.get_user_party(3) is None)
    check("유효 파티는 유지됨", repo.get_party("Overwatch") is not None)



def test_party_capacity_constraint():
    print("\n[4] 파티 정원·역할 SQL 제약")
    with tempfile.TemporaryDirectory() as directory:
        repo = SQLitePartyRepository(pathlib.Path(directory) / "p.db")
        repo = _bind(repo)
        repo.create_party("PUBG", 1_000)
        results = [repo.add_participant("PUBG", uid, None, max_players=2) for uid in (1, 2, 3)]
        check("정원 초과는 DB에서 거부", results == [True, True, False], f"({results})")

        repo.create_party("LoL", 1_000)
        check("역할 배정", repo.add_participant("LoL", 10, "탑", max_players=5) is True)
        check("역할 중복은 DB에서 거부", repo.add_participant("LoL", 11, "탑", max_players=5) is False)
        check("본인 역할 재지정은 허용", repo.add_participant("LoL", 10, "탑", max_players=5) is True)
        check("거부된 참가는 행을 남기지 않음", repo.get_user_party(11) is None)
        check("거부 후에도 기존 배정 유지", repo.get_participants("LoL") == {10: "탑"})



def test_party_cog_uses_epoch_seconds():
    from module.playwith_cog import PlayWithCog

    class RecordingRepository:
        created_at = None
        cutoff = None

        def create_party(self, guild_id, game, created_at, host_id=None):
            self.created_at = created_at
            return True

        def list_expired_parties(self, cutoff):
            self.cutoff = cutoff
            return []

        def delete_party_if_expired(self, guild_id, game, cutoff):
            return False

    repository = RecordingRepository()
    cog = object.__new__(PlayWithCog)
    cog.db = repository
    with patch("time.time", return_value=2_000_000_000.75):
        asyncio.run(cog.create_party(_TEST_GUILD, "LOL"))
        asyncio.run(PlayWithCog.cleanup_parties.coro(cog))

    check("Cog 파티 생성 시 epoch 정수 전달", repository.created_at == 2_000_000_000)
    check(
        "Cog 만료 정리 시 24시간 전 epoch 정수 전달",
        repository.cutoff == 1_999_913_600,
    )


def test_persistent_party_panel_contract():
    import module.playwith_cog as playwith_cog

    source = inspect.getsource(playwith_cog)
    check(
        "구 파티 slash command 제거",
        all(f'name="{name}"' not in source for name in ("모집", "파티", "나가기", "변경")),
    )
    check(
        "파티 패널 custom_id는 SHA-256 digest 사용",
        "hashlib.sha256" in source and "_game_key(game)" in source,
    )
    check(
        "파티 패널은 startup/setup/cleanup 복구 경로 제공",
        all(name in playwith_cog.PlayWithCog.__dict__ for name in (
            "ensure_panels", "render_game_panel", "on_ready", "on_member_remove"
        )),
    )


def test_factory():
    print("\n[5] Repository 팩토리")
    # sqlite 백엔드 (기본값)
    a_repo = create_attendance_repository()
    p_repo = create_party_repository()
    check("sqlite 백엔드: AttendanceRepository 생성", isinstance(a_repo, SQLiteAttendanceRepository))
    check("sqlite 백엔드: PartyRepository 생성", isinstance(p_repo, SQLitePartyRepository))
    check("DB 파일이 DATA_DIR 아래에 생성됨", a_repo.db_path.parent == _TMP_DIR.resolve())

    # 미지원 백엔드는 명확한 에러를 내야 함
    original = database.DB_BACKEND
    database.DB_BACKEND = "oracle"
    try:
        try:
            create_attendance_repository()
            check("미지원 백엔드 거부 (NotImplementedError)", False)
        except NotImplementedError:
            check("미지원 백엔드 거부 (NotImplementedError)", True)
    finally:
        database.DB_BACKEND = original


def test_cog_facade():
    print("\n[6] AttendanceCog 파사드 (Repository 주입)")
    raw = SQLiteAttendanceRepository(_TMP_DIR / "facade_test.db")
    repo = _bind(raw)
    cog = AttendanceCog(bot=None, repository=raw)
    G = _TEST_GUILD

    # 파사드는 모두 async다. 동기 리포지토리를 스레드로 넘겨 이벤트 루프를 지킨다.
    asyncio.run(cog.add_points(G, 42, 1_500))
    check("add_points 위임", repo.get_points(42) == 1_500)
    check("get_points 위임", asyncio.run(cog.get_points(G, 42)) == 1_500)
    check(
        "deduct_points 위임",
        asyncio.run(cog.deduct_points(G, 42, 500)) is True and repo.get_points(42) == 1_000,
    )

    asyncio.run(cog.increment_forbidden_count(G, 42))
    asyncio.run(cog.increment_forbidden_count(G, 42))
    check("forbidden_count 위임", asyncio.run(cog.get_forbidden_count(G, 42)) == 2)
    kst_now = datetime.datetime(2026, 8, 4, 23, 59, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
    with patch("module.attendance_cog.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = kst_now
        reservation = asyncio.run(cog.reserve_ai_usage(42, "light", 3))
        check("KST 날짜로 AI 사용량 예약", reservation == ("2026-08-04", 1))
        check("AI 사용량 조회 위임", asyncio.run(cog.get_ai_usage(42, "light")) == 1)
        check("AI 사용량 반환 위임", asyncio.run(cog.release_ai_usage(42, "2026-08-04", "light")) is True)
    check(
        "파사드 전체가 코루틴",
        all(
            inspect.iscoroutinefunction(getattr(AttendanceCog, name))
            for name in ("get_points", "add_points", "deduct_points",
                         "get_ledger", "reserve_ai_usage", "release_ai_usage",
                         "get_ai_usage", "increment_forbidden_count", "get_forbidden_count")
        ),
    )


def test_channel_sessions():
    print("\n[7] 채널별 대화 세션 분리")
    cog = HyacineChatCog(bot=None)

    s1 = cog.get_session(111)
    s2 = cog.get_session(222)

    check("두 채널은 서로 다른 세션 객체", s1 is not s2)
    check("같은 채널은 같은 세션 재사용", cog.get_session(111) is s1)
    # 히스토리 분리
    s1.history.append({"role": "user", "content": "채널 111의 비밀 이야기"})
    s1.history.append({"role": "assistant", "content": "네, 기억할게요"})
    cog.trim(s1)

    s2_contents = [m["content"] for m in s2.history if m["role"] != "system"]
    check("채널 111의 대화가 채널 222에 노출되지 않음", len(s2_contents) == 0)
    s1_contents = [m["content"] for m in s1.history if m["role"] != "system"]
    check("채널 111 히스토리는 유지됨", "채널 111의 비밀 이야기" in s1_contents)

    # trim이 system 프롬프트를 보존하는지
    check("trim 후 system 프롬프트 보존", any(m["role"] == "system" for m in s1.history))

    lru_cog = HyacineChatCog(bot=None)
    for channel_id in range(lru_cog.MAX_CHANNEL_SESSIONS):
        lru_cog.get_session(channel_id)
    lru_cog.get_session(0)
    lru_cog.get_session(lru_cog.MAX_CHANNEL_SESSIONS)
    check("채널 세션 수 제한", len(lru_cog.sessions) == lru_cog.MAX_CHANNEL_SESSIONS)
    check("최근 사용 세션 유지", 0 in lru_cog.sessions)
    check("가장 오래된 세션 제거", 1 not in lru_cog.sessions)


def test_imports():
    print("\n[8] 전체 모듈 import 스모크 테스트")
    import importlib
    mods = [
        "module.config",
        "module.database",
        "module.main",
        "module.music_cog",
        "module.panel",
        "module.guildsettings_cog",
        "module.attendance_cog",
        "module.playwith_cog",
        "module.eventnotice_cog",
        "module.forbiddenfilter_cog",
        "module.webadmin_cog",
        "module.hyacine_chat_cog",
        "module.hyacine_image_cog",
        "module.finance_cog",
    ]
    for m in mods:
        try:
            importlib.import_module(m)
            check(f"import {m}", True)
        except Exception as e:
            check(f"import {m}", False, f"({e})")


def test_music_core_contract():
    print("\n[9] 음악 core·영속 패널")
    import module.main as bot_main
    import module.music_cog as music_cog

    declared = [
        line.split("#")[0].strip()
        for line in (PROJECT_ROOT / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    declared = [line for line in declared if line]
    check("PyNaCl은 1.5 이상 2 미만으로 고정", "PyNaCl>=1.5,<2" in declared)
    check(
        # 외부 사이트가 바뀌면 즉시 최신 yt-dlp로 올라갈 수 있어야 한다.
        "yt-dlp는 상한도 exact pin도 없음",
        [line for line in declared if line.lower().startswith("yt-dlp")] == ["yt-dlp"],
    )

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    apt_packages = [
        token
        for token in dockerfile.split("apt-get install")[-1].split("&&")[0].split()
        if not token.startswith("-") and token != "\\"
    ]
    check("Dockerfile은 distro 패키지로 ffmpeg만 설치", apt_packages == ["ffmpeg"])
    check(
        "apt cache를 이미지에 남기지 않음",
        "rm -rf /var/lib/apt/lists/*" in dockerfile,
    )

    # 최상위(들여쓰기 0) import만 본다. `from yt_dlp import YoutubeDL`처럼 형태만 바꾼
    # 회귀도 잡아야 하므로 문자열 하나가 아니라 import 줄 전체를 훑는다.
    top_level_imports = [
        line
        for line in inspect.getsource(music_cog).splitlines()
        if (line.startswith("import ") or line.startswith("from "))
        and ("yt_dlp" in line or "nacl" in line)
    ]
    check(
        "yt-dlp/PyNaCl은 최상위에서 import하지 않음 (미설치 환경에서도 import 가능)",
        top_level_imports == [],
        f"({top_level_imports})",
    )
    check(
        "의존성 검사를 가진 확장은 음악뿐",
        [name for name, _, dep in bot_main.EXTENSIONS if dep is not None]
        == ["module.music_cog"],
    )

    with patch.object(music_cog, "find_spec", lambda name, *a, **k: None):
        reason = music_cog.music_dependency_error()
    check(
        "skip 사유에 빠진 package와 설치 안내 포함",
        reason is not None
        and "PyNaCl" in reason
        and "yt-dlp" in reason
        and "pip install" in reason,
        f"({reason})",
    )
    with patch.object(music_cog, "find_spec", lambda name, *a, **k: object()):
        check("의존성이 갖춰지면 skip 사유 없음", music_cog.music_dependency_error() is None)

    view = music_cog.MusicPanelView(SimpleNamespace())
    check("음악 패널 view는 persistent", view.is_persistent())
    check(
        "음악 패널 button custom_id 고정",
        {item.custom_id for item in view.children}
        == {"music:add", "music:skip", "music:pause", "music:stop", "music:remove"},
    )
    check("패널 queue 표시는 10곡", music_cog.MAX_QUEUE_DISPLAY == 10)
    check("remove select는 25곡", music_cog.MAX_REMOVE_OPTIONS == 25)
    check(
        "패널 복구·렌더 interface 존재",
        all(callable(getattr(music_cog.MusicCog, name, None)) for name in ("ensure_panel", "render_panel")),
    )
    source = inspect.getsource(music_cog)
    check("주기적 음악 progress update 없음", "@tasks.loop" not in source)

    # 재생 엔진의 동작 계약(상태 전이, ffmpeg option, 불변 track)은
    # test_discord_commands.py의 MusicPlayerStateTests/MusicPanelTests가 실행해 검증한다.


def test_backup_round_trip():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR
    backup.BACKUP_DIR = _TMP_DIR / "backups"
    backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
    SQLiteAttendanceRepository(_TMP_DIR / "attendance_data.db").add_points(_TEST_GUILD, 77, 1234)
    SQLitePartyRepository(_TMP_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(_TMP_DIR / "guild_settings.db")

    manifest = backup.create_backup_set()
    result = backup.verify_backup_set(manifest)
    check("출석 DB 백업 검증", result["attendance_data.db"]["users"] == 1)
    check("파티 DB 백업 검증", result["party_data.db"]["parties"] == 1)
    backup.restore_test(manifest)
    check("백업 복구 테스트", True)


def test_settings_backup_round_trip():
    import module.backup as backup

    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        data_dir = root / "data"
        backup_dir = root / "backups"
        settings_dir = root / "settings"
        stage = root / "stage"
        data_dir.mkdir()
        settings_dir.mkdir()
        SQLiteAttendanceRepository(data_dir / "attendance_data.db")
        SQLitePartyRepository(data_dir / "party_data.db")
        SQLiteGuildSettingsRepository(data_dir / "guild_settings.db")
        expected = {
            "persona.json": b'{"system_prompt":"p","greeting":"g"}\n',
            "forbidden_words.json": b'["x"]\n',
            "games.json": b'{"Game":{"max_players":2,"roles":[]}}\n',
        }
        for name, content in expected.items():
            (settings_dir / name).write_bytes(content)

        with (
            patch.object(backup, "DATA_DIR", data_dir),
            patch.object(backup, "BACKUP_DIR", backup_dir),
            patch.object(backup, "SETTINGS_DIR", settings_dir),
            patch.object(backup, "BACKUP_INTERVAL_SECONDS", 21600),
            patch.object(backup, "BACKUP_RETENTION_DAYS", 30),
        ):
            manifest = backup.create_backup_set(
                datetime.datetime(2026, 8, 4, tzinfo=datetime.timezone.utc)
            )
            document = json.loads(manifest.read_text(encoding="utf-8"))
            backup.stage_restore(manifest, stage)

            empty_settings = root / "empty-settings"
            empty_settings.mkdir()
            with patch.object(backup, "SETTINGS_DIR", empty_settings):
                missing_manifest = backup.create_backup_set(
                    datetime.datetime(2026, 8, 6, tzinfo=datetime.timezone.utc)
                )
            missing_document = json.loads(
                missing_manifest.read_text(encoding="utf-8")
            )

            historical_document = json.loads(manifest.read_text(encoding="utf-8"))
            historical_document.pop("settings")
            historical_manifest = backup_dir / "historical-db-only-manifest.json"
            historical_manifest.write_text(
                json.dumps(historical_document), encoding="utf-8"
            )
            historical_stage = root / "historical-stage"
            try:
                historical_verified = backup.verify_backup_set(historical_manifest)
                backup.stage_restore(historical_manifest, historical_stage)
                historical_ok = bool(historical_verified) and not (
                    historical_stage / "settings"
                ).exists()
            except RuntimeError:
                historical_ok = False

            corruption_results = []
            for field, value in (
                ("size", document["settings"][0]["size"] + 1),
                ("sha256", "0" * 64),
            ):
                invalid_document = json.loads(json.dumps(document))
                invalid_document["settings"][0][field] = value
                invalid_manifest = backup_dir / f"invalid-settings-{field}.json"
                invalid_manifest.write_text(
                    json.dumps(invalid_document), encoding="utf-8"
                )
                invalid_stage = root / f"invalid-settings-{field}"
                try:
                    backup.stage_restore(invalid_manifest, invalid_stage)
                    rejected = False
                except RuntimeError:
                    rejected = True
                corruption_results.append(rejected and not invalid_stage.exists())

            invalid_settings = (
                ("설정 상위 경로 거부", "../persona.json"),
                ("설정 source 중복 거부", document["settings"][0]["source"]),
                ("허용되지 않은 설정 거부", "secret.json"),
            )
            invalid_results = []
            for index, (_, source) in enumerate(invalid_settings):
                invalid_document = json.loads(json.dumps(document))
                invalid_document["settings"].append(
                    {
                        "source": source,
                        "backup": document["settings"][1]["backup"],
                        "size": document["settings"][1]["size"],
                        "sha256": document["settings"][1]["sha256"],
                    }
                )
                invalid_manifest = backup_dir / f"invalid-setting-source-{index}.json"
                invalid_manifest.write_text(
                    json.dumps(invalid_document), encoding="utf-8"
                )
                try:
                    backup.verify_backup_set(invalid_manifest)
                    invalid_results.append(False)
                except RuntimeError:
                    invalid_results.append(True)

            unrelated_backup = backup_dir / "unrelated-settings-backup.json"
            unrelated_backup.write_bytes(b"unrelated")
            unrelated_document = json.loads(json.dumps(document))
            unrelated_document["settings"] = [
                {
                    "source": "persona.json",
                    "backup": unrelated_backup.name,
                    "size": unrelated_backup.stat().st_size,
                    "sha256": backup._sha256(unrelated_backup),
                }
            ]
            unrelated_manifest = backup_dir / "20260809T000000Z-manifest.json"
            unrelated_manifest.write_text(
                json.dumps(unrelated_document), encoding="utf-8"
            )
            try:
                backup.verify_backup_set(unrelated_manifest)
                unrelated_backup_rejected = False
            except RuntimeError:
                unrelated_backup_rejected = True

            symlink_stage = root / "symlink-stage"
            outside_settings = root / "outside-settings"
            symlink_stage.mkdir()
            outside_settings.mkdir()
            (symlink_stage / "settings").symlink_to(
                outside_settings, target_is_directory=True
            )
            try:
                backup.stage_restore(manifest, symlink_stage)
                stage_symlink_rejected = False
            except RuntimeError:
                stage_symlink_rejected = True

            stage_race = root / "stage-directory-race"
            stage_race_outside = root / "stage-race-outside"
            detached_settings = root / "detached-settings"
            stage_race_outside.mkdir()
            real_open = backup.os.open
            swapped_stage = False

            def swap_stage_before_destination(path, *args, **kwargs):
                nonlocal swapped_stage
                if (
                    not swapped_stage
                    and kwargs.get("dir_fd") is not None
                    and path == "persona.json"
                ):
                    settings_path = stage_race / "settings"
                    settings_path.rename(detached_settings)
                    settings_path.symlink_to(
                        stage_race_outside, target_is_directory=True
                    )
                    swapped_stage = True
                return real_open(path, *args, **kwargs)

            with patch.object(backup.os, "open", side_effect=swap_stage_before_destination):
                try:
                    backup.stage_restore(manifest, stage_race)
                    stage_race_safe = (
                        swapped_stage
                        and not (stage_race_outside / "persona.json").exists()
                        and all(
                            (detached_settings / name).read_bytes() == content
                            for name, content in expected.items()
                        )
                    )
                except RuntimeError:
                    stage_race_safe = False

            race_source = root / "race-source.json"
            race_original = root / "race-original.json"
            race_target = root / "race-target.json"
            race_copy = root / "race-copy.json"
            race_source.write_bytes(b"regular")
            race_target.write_bytes(b"outside")
            real_open = backup.os.open

            def replace_source_before_open(path, *args, **kwargs):
                if pathlib.Path(path) == race_source:
                    race_source.replace(race_original)
                    race_source.symlink_to(race_target)
                return real_open(path, *args, **kwargs)

            with patch.object(backup.os, "open", side_effect=replace_source_before_open):
                try:
                    backup._copy_setting(race_source, race_copy)
                    race_rejected = False
                except RuntimeError:
                    race_rejected = True

            symlink_settings = root / "symlink-settings"
            symlink_settings.mkdir()
            target = root / "settings-target.json"
            target.write_bytes(expected["persona.json"])
            (symlink_settings / "persona.json").symlink_to(target)
            with patch.object(backup, "SETTINGS_DIR", symlink_settings):
                try:
                    backup.create_backup_set(
                        datetime.datetime(2026, 8, 7, tzinfo=datetime.timezone.utc)
                    )
                    symlink_rejected = False
                except RuntimeError:
                    symlink_rejected = True

            directory_settings = root / "directory-settings"
            directory_settings.mkdir()
            (directory_settings / "persona.json").mkdir()
            with patch.object(backup, "SETTINGS_DIR", directory_settings):
                try:
                    backup.create_backup_set(
                        datetime.datetime(2026, 8, 8, tzinfo=datetime.timezone.utc)
                    )
                    directory_rejected = False
                except RuntimeError:
                    directory_rejected = True

            expired_manifest = backup.create_backup_set(
                datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
            )
            expired_document = json.loads(expired_manifest.read_text(encoding="utf-8"))
            deleted = backup.prune_backups(
                datetime.datetime(2026, 8, 4, tzinfo=datetime.timezone.utc)
            )

        check("설정 백업 대상 목록", backup.SETTINGS_FILES == tuple(expected))
        check(
            "manifest 설정 3개",
            {item["source"] for item in document["settings"]} == set(expected),
        )
        check(
            "설정 stage byte 보존",
            all(
                (stage / "settings" / name).read_bytes() == content
                for name, content in expected.items()
            ),
        )
        check(
            "설정 파일이 없어도 빈 manifest 백업 성공",
            missing_document["settings"] == [],
        )
        check("DB-only historical manifest 복구", historical_ok)
        check("설정 크기와 checksum은 stage 전 거부", all(corruption_results))
        check("안전하지 않거나 중복된 설정 manifest 거부", all(invalid_results))
        check("설정 backup은 source별 canonical 이름만 허용", unrelated_backup_rejected)
        check(
            "symlink 설정 stage는 stage 밖에 쓰지 않고 거부",
            stage_symlink_rejected and not (outside_settings / "persona.json").exists(),
        )
        check("설정 stage 교체 race는 stage 밖에 쓰지 않음", stage_race_safe)
        check(
            "설정 source 교체 symlink race 거부",
            race_rejected
            and not race_copy.exists()
            and race_target.read_bytes() == b"outside",
        )
        check("설정 symlink 백업 거부", symlink_rejected)
        check("설정 디렉터리 백업 거부", directory_rejected)
        check(
            "설정 포함 만료 backup set 정리",
            deleted == 1
            and not expired_manifest.exists()
            and all(
                not (backup_dir / item["backup"]).exists()
                for item in expired_document["settings"]
            ),
        )


def test_legacy_backup_restore_and_prune():
    import module.backup as backup

    root = _TMP_DIR / "legacy-backup-lifecycle"
    data_dir = root / "data"
    backup_dir = root / "backups"
    backup.DATA_DIR = data_dir
    backup.BACKUP_DIR = backup_dir
    backup.BACKUP_RETENTION_DAYS = 30
    timestamp = "20260101T000000Z"
    _create_legacy_attendance_db(data_dir / "attendance_data.db")
    party_path = data_dir / "party_data.db"
    settings_path = data_dir / "guild_settings.db"
    SQLitePartyRepository(party_path).create_party(
        _TEST_GUILD, "LOL", 2_000_000_000
    )
    with closing(sqlite3.connect(settings_path)) as conn:
        conn.execute(
            "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, recruit_channel_id INTEGER, event_channel_id INTEGER)"
        )
        conn.execute("INSERT INTO guild_settings VALUES (7, 700, 701)")
        conn.execute("PRAGMA user_version = 1")
    with closing(sqlite3.connect(party_path)) as conn:
        conn.execute("PRAGMA user_version = 0")
    backup_dir.mkdir(parents=True)

    items = []
    for source_name, current_tables in backup.DATABASES.items():
        source = data_dir / source_name
        copied = backup_dir / f"{timestamp}-{source_name}"
        backup._backup_one(source, copied)
        tables = (
            {"users", "point_ledger"}
            if source_name == "attendance_data.db"
            else {"guild_settings"}
            if source_name == "guild_settings.db"
            else current_tables
        )
        with closing(sqlite3.connect(copied)) as conn:
            counts = {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in sorted(tables)
            }
        items.append(
            {
                "source": source_name,
                "backup": copied.name,
                "size": copied.stat().st_size,
                "sha256": backup._sha256(copied),
                "tables": counts,
            }
        )

    manifest = backup_dir / f"{timestamp}-manifest.json"
    manifest.write_text(
        json.dumps({"created_at": "2026-01-01T00:00:00+00:00", "databases": items}),
        encoding="utf-8",
    )
    try:
        verified = backup.verify_backup_set(manifest)
    except RuntimeError:
        verified = None
    check(
        "historical v1 attendance backup verifies with base tables",
        verified is not None
        and verified["attendance_data.db"] == {"point_ledger": 1, "users": 1},
    )
    legacy_v0 = root / "legacy-v0-attendance.db"
    shutil.copy2(data_dir / "attendance_data.db", legacy_v0)
    with closing(sqlite3.connect(legacy_v0)) as conn:
        conn.execute("PRAGMA user_version = 0")
    try:
        v0_counts = backup.verify_database(
            legacy_v0,
            backup.DATABASES["attendance_data.db"],
            source_name="attendance_data.db",
            allow_legacy=True,
        )
    except RuntimeError:
        v0_counts = None
    check(
        "historical v0 attendance accepts only its base-table contract",
        v0_counts == {"point_ledger": 1, "users": 1},
    )

    stage_restore = getattr(backup, "stage_restore", None)
    check("historical restore stages through migration API", stage_restore is not None)
    if stage_restore is None:
        return

    invalid_document = json.loads(manifest.read_text(encoding="utf-8"))
    invalid_document["databases"][0]["sha256"] = "0" * 64
    invalid_manifest = backup_dir / "invalid-manifest.json"
    invalid_manifest.write_text(json.dumps(invalid_document), encoding="utf-8")
    invalid_stage = root / "invalid-stage"
    try:
        stage_restore(invalid_manifest, invalid_stage)
        checksum_rejected = False
    except RuntimeError:
        checksum_rejected = True
    check(
        "checksum failure precedes any staged mutation",
        checksum_rejected and not invalid_stage.exists(),
    )

    stage = root / "stage"
    try:
        stage_restore(manifest, stage)
        staged = True
    except RuntimeError:
        staged = False
    check("all staged legacy databases reach the current contract", staged)
    if not staged:
        return
    staged_attendance = stage / "attendance_data.db"
    with closing(sqlite3.connect(staged_attendance)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        points = conn.execute(
            "SELECT points FROM users WHERE guild_id = 7 AND user_id = 8"
        ).fetchone()
    check(
        "staged historical attendance migrates before installation",
        version == 2 and "ai_usage" in tables and points == (9000,),
    )
    staged_settings = stage / "guild_settings.db"
    with closing(sqlite3.connect(staged_settings)) as conn:
        settings_version = conn.execute("PRAGMA user_version").fetchone()[0]
        settings_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        party_channel = conn.execute(
            "SELECT party_channel_id FROM guild_settings WHERE guild_id = 7"
        ).fetchone()
    check(
        "staged historical settings migrates to the panel contract",
        settings_version == 2
        and "party_panels" in settings_tables
        and party_channel == (700,),
    )
    future = root / "future-attendance.db"
    shutil.copy2(staged_attendance, future)
    with closing(sqlite3.connect(future)) as conn:
        conn.execute("PRAGMA user_version = 999")
    try:
        backup.verify_database(
            future,
            backup.DATABASES["attendance_data.db"],
            source_name="attendance_data.db",
            allow_legacy=True,
        )
        future_rejected = False
    except RuntimeError:
        future_rejected = True
    check("historical verification rejects future schemas", future_rejected)

    deleted = backup.prune_backups(
        datetime.datetime(2026, 8, 4, tzinfo=datetime.timezone.utc)
    )
    check(
        "expired valid historical backup is pruned",
        deleted == 1
        and not manifest.exists()
        and all(not (backup_dir / item["backup"]).exists() for item in items),
    )


def test_invalid_backup_settings_prevent_creation():
    import module.backup as backup

    for name, value in (
        ("BACKUP_INTERVAL_SECONDS", 0),
        ("BACKUP_INTERVAL_SECONDS", -1),
        ("BACKUP_RETENTION_DAYS", 0),
        ("BACKUP_RETENTION_DAYS", -1),
    ):
        backup.DATA_DIR = _TMP_DIR / f"invalid-create-data-{name}-{value}"
        backup.BACKUP_DIR = _TMP_DIR / f"invalid-create-{name}-{value}"
        backup.BACKUP_INTERVAL_SECONDS = 21600
        backup.BACKUP_RETENTION_DAYS = 30
        setattr(backup, name, value)

        try:
            backup.create_backup_set()
            rejected = False
        except Exception as exc:
            rejected = isinstance(exc, RuntimeError) and name in str(exc)

        check(f"{name}={value} 백업 생성 거부", rejected)
        check(
            f"{name}={value} 생성 부작용 없음",
            not backup.BACKUP_DIR.exists(),
        )
    backup.BACKUP_INTERVAL_SECONDS = 21600
    backup.BACKUP_RETENTION_DAYS = 30


def test_invalid_retention_prevents_pruning():
    import module.backup as backup

    for value in (0, -1):
        backup.DATA_DIR = _TMP_DIR / f"invalid-prune-data-{value}"
        backup.BACKUP_DIR = _TMP_DIR / f"invalid-prune-backups-{value}"
        backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
        backup.BACKUP_INTERVAL_SECONDS = 21600
        backup.BACKUP_RETENTION_DAYS = 30
        SQLiteAttendanceRepository(
            backup.DATA_DIR / "attendance_data.db"
        ).add_points(_TEST_GUILD, 1, 100)
        SQLitePartyRepository(
            backup.DATA_DIR / "party_data.db"
        ).create_party(_TEST_GUILD, "LOL", 2_000_000_000)
        SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
        created_at = datetime.datetime(
            2026,
            1,
            1,
            tzinfo=datetime.timezone.utc,
        )
        manifest = backup.create_backup_set(created_at)
        backup.BACKUP_RETENTION_DAYS = value

        try:
            backup.prune_backups(created_at + datetime.timedelta(days=1))
            rejected = False
        except RuntimeError as exc:
            rejected = "BACKUP_RETENTION_DAYS" in str(exc)

        check(f"BACKUP_RETENTION_DAYS={value} 정리 거부", rejected)
        check(f"BACKUP_RETENTION_DAYS={value} 백업 보존", manifest.exists())
    backup.BACKUP_RETENTION_DAYS = 30


def test_invalid_interval_prevents_loop_entry():
    import module.backup as backup

    for value in (0, -1):
        backup.DATA_DIR = _TMP_DIR / f"invalid-loop-data-{value}"
        backup.BACKUP_DIR = _TMP_DIR / f"invalid-loop-backups-{value}"
        backup.BACKUP_INTERVAL_SECONDS = value
        backup.BACKUP_RETENTION_DAYS = 30

        with (
            patch.object(sys, "argv", ["backup", "loop"]),
            patch.object(
                backup.time,
                "sleep",
                side_effect=AssertionError("backup loop entered"),
            ),
        ):
            try:
                backup.main()
                rejected = False
            except Exception as exc:
                rejected = (
                    isinstance(exc, RuntimeError)
                    and "BACKUP_INTERVAL_SECONDS" in str(exc)
                )

        check(f"BACKUP_INTERVAL_SECONDS={value} loop 거부", rejected)
        check(
            f"BACKUP_INTERVAL_SECONDS={value} loop 부작용 없음",
            not backup.BACKUP_DIR.exists(),
        )
    backup.BACKUP_INTERVAL_SECONDS = 21600


def test_backup_loop_pid_lifecycle():
    import module.backup as backup

    backup_dir = _TMP_DIR / "loop-pid-backups"
    backup_dir.mkdir(exist_ok=True)
    pid_path = backup_dir / ".backup.pid"
    pid_path.unlink(missing_ok=True)
    observed = []

    def stop_loop():
        observed.append(
            pid_path.read_text(encoding="ascii").strip()
            if pid_path.exists()
            else "missing"
        )
        raise KeyboardInterrupt

    with patch.object(backup, "BACKUP_DIR", backup_dir), \
         patch.object(backup, "_validate_backup_settings"), \
         patch.object(backup, "create_backup_set", side_effect=stop_loop), \
         patch.object(sys, "argv", ["backup", "loop"]), \
         patch.object(sys, "platform", "darwin"):
        try:
            backup.main()
        except KeyboardInterrupt:
            pass

    check("backup loop 실행 중 현재 PID 게시", observed == [str(os.getpid())])
    check("backup loop 정상 unwind 시 PID 파일 정리", not pid_path.exists())


def test_pid_file_darwin_publishes_and_cleans():
    import module.backup as backup

    root = _TMP_DIR / "darwin-pid"
    root.mkdir(exist_ok=True)
    pid_path = root / ".service.pid"
    lock_path = root / ".service.pid.lock"
    temporary_path = root / ".service.pid.tmp"
    pid_path.unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
    temporary_path.unlink(missing_ok=True)

    with patch.object(sys, "platform", "darwin"):
        with backup.pid_file(pid_path):
            published = pid_path.read_text(encoding="ascii")
            lock_held = lock_path.exists()
            temporary_absent = not temporary_path.exists()

    check("Darwin PID context는 현재 PID를 게시", published == f"{os.getpid()}\n")
    check("Darwin PID context는 companion lock을 사용", lock_held)
    check(
        "Darwin PID 정상 종료는 PID/임시 파일만 정리",
        not pid_path.exists()
        and temporary_absent
        and not temporary_path.exists()
        and lock_path.exists(),
    )


def test_pid_file_linux_is_noop_without_artifacts():
    import module.backup as backup

    root = _TMP_DIR / "linux-pid"
    pid_path = root / ".service.pid"
    with patch.object(sys, "platform", "linux"):
        with backup.pid_file(pid_path):
            clean_inside = not root.exists()

    check(
        "Linux PID context는 PID/lock/디렉터리를 만들지 않음",
        clean_inside and not root.exists(),
    )


def test_pid_file_rejects_contention_before_handoff():
    import module.backup as backup

    root = _TMP_DIR / "contended-pid"
    root.mkdir(exist_ok=True)
    pid_path = root / ".service.pid"
    pid_path.unlink(missing_ok=True)

    with patch.object(sys, "platform", "darwin"):
        with backup.pid_file(pid_path):
            try:
                with backup.pid_file(pid_path):
                    pass
                rejected = False
            except RuntimeError:
                rejected = True
            first_publication_intact = (
                pid_path.exists()
                and pid_path.read_text(encoding="ascii") == f"{os.getpid()}\n"
            )
        with backup.pid_file(pid_path):
            next_holder_published = (
                pid_path.read_text(encoding="ascii") == f"{os.getpid()}\n"
            )

    check(
        "같은 PID 경로의 두 번째 holder 거부",
        rejected and first_publication_intact,
    )
    check(
        "첫 holder cleanup 뒤 다음 holder만 게시",
        next_holder_published and not pid_path.exists(),
    )


def test_pid_file_replaces_stale_after_lock_release():
    import module.backup as backup

    root = _TMP_DIR / "stale-pid"
    root.mkdir(exist_ok=True)
    pid_path = root / ".service.pid"
    lock_path = root / ".service.pid.lock"
    pid_path.write_text("12345\n", encoding="ascii")
    with lock_path.open("a+b") as stale_lock:
        backup.fcntl.flock(stale_lock, backup.fcntl.LOCK_EX)
        backup.fcntl.flock(stale_lock, backup.fcntl.LOCK_UN)

    with patch.object(sys, "platform", "darwin"):
        with backup.pid_file(pid_path):
            replaced = pid_path.read_text(encoding="ascii")

    check(
        "release된 lock의 stale PID를 현재 PID로 교체",
        replaced == f"{os.getpid()}\n" and not pid_path.exists(),
    )


def test_pid_file_replace_occurs_while_companion_lock_is_held():
    import module.backup as backup

    root = _TMP_DIR / "replace-order-pid"
    root.mkdir(exist_ok=True)
    pid_path = root / ".service.pid"
    lock_path = root / ".service.pid.lock"
    events = []
    real_flock = backup.fcntl.flock
    real_replace = backup.os.replace

    def recording_flock(lock, operation):
        result = real_flock(lock, operation)
        if operation & backup.fcntl.LOCK_EX:
            events.append(("flock", operation))
        return result

    def recording_replace(source, destination):
        with lock_path.open("a+b") as contender:
            try:
                real_flock(
                    contender,
                    backup.fcntl.LOCK_EX | backup.fcntl.LOCK_NB,
                )
                held = False
                real_flock(contender, backup.fcntl.LOCK_UN)
            except BlockingIOError:
                held = True
        events.append(("replace", held))
        return real_replace(source, destination)

    with patch.object(sys, "platform", "darwin"), \
         patch.object(backup.fcntl, "flock", side_effect=recording_flock), \
         patch.object(backup.os, "replace", side_effect=recording_replace):
        with backup.pid_file(pid_path):
            pass

    replace_events = [event for event in events if event[0] == "replace"]
    check(
        "PID publication은 flock 획득 뒤 os.replace로 수행",
        len(replace_events) == 1
        and replace_events[0] == ("replace", True)
        and events.index(replace_events[0]) > 0
        and events[0][0] == "flock",
        f"({events})",
    )


def test_pid_file_unlinks_before_companion_lock_release():
    import module.backup as backup

    root = _TMP_DIR / "unlink-order-pid"
    root.mkdir(exist_ok=True)
    pid_path = root / ".service.pid"
    lock_path = root / ".service.pid.lock"
    cleanup_lock_states = []
    real_unlink = pathlib.Path.unlink

    def recording_unlink(path, *args, **kwargs):
        if path == pid_path:
            with lock_path.open("a+b") as contender:
                try:
                    backup.fcntl.flock(
                        contender,
                        backup.fcntl.LOCK_EX | backup.fcntl.LOCK_NB,
                    )
                    held = False
                    backup.fcntl.flock(contender, backup.fcntl.LOCK_UN)
                except BlockingIOError:
                    held = True
            cleanup_lock_states.append(held)
        return real_unlink(path, *args, **kwargs)

    with patch.object(sys, "platform", "darwin"), \
         patch.object(pathlib.Path, "unlink", new=recording_unlink):
        with backup.pid_file(pid_path):
            pass

    check(
        "PID 정상 cleanup은 companion lock 해제 전에 unlink",
        cleanup_lock_states == [True] and not pid_path.exists(),
        f"({cleanup_lock_states})",
    )


def test_pid_file_recovers_after_actual_abnormal_child_exit():
    import module.backup as backup

    root = _TMP_DIR / "abnormal-exit-pid"
    root.mkdir(exist_ok=True)
    pid_path = root / ".service.pid"
    lock_path = root / ".service.pid.lock"
    child_code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import module.backup as backup\n"
        "backup.sys.platform = 'darwin'\n"
        "with backup.pid_file(Path(sys.argv[1])):\n"
        "    os._exit(0)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(pid_path)],
        cwd=PROJECT_ROOT,
    )
    child_pid = child.pid
    timed_out = False
    try:
        try:
            exit_code = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.kill()
            exit_code = child.wait()
        stale_contents = (
            pid_path.read_text(encoding="ascii") if pid_path.exists() else None
        )

        with lock_path.open("a+b") as probe:
            try:
                backup.fcntl.flock(
                    probe,
                    backup.fcntl.LOCK_EX | backup.fcntl.LOCK_NB,
                )
                released = True
                backup.fcntl.flock(probe, backup.fcntl.LOCK_UN)
            except BlockingIOError:
                released = False

        with patch.object(sys, "platform", "darwin"):
            with backup.pid_file(pid_path):
                replacement = pid_path.read_text(encoding="ascii")

        check(
            "os._exit child는 stale PID를 남기고 kernel lock은 해제",
            not timed_out
            and exit_code == 0
            and stale_contents == f"{child_pid}\n"
            and released,
        )
        check(
            "abnormal exit 뒤 다음 holder가 stale PID 교체 후 정상 정리",
            replacement == f"{os.getpid()}\n" and not pid_path.exists(),
        )
    finally:
        try:
            if child.poll() is None:
                child.kill()
        finally:
            child.wait()


def test_pid_file_publication_failure_cleans_temp_lock_and_fds():
    import module.backup as backup

    root = _TMP_DIR / "failed-publication-pid"
    root.mkdir(exist_ok=True)
    pid_path = root / ".service.pid"
    temporary_path = root / ".service.pid.tmp"
    fd_root = "/dev/fd" if pathlib.Path("/dev/fd").exists() else "/proc/self/fd"
    open_fds_before = len(os.listdir(fd_root))

    with patch.object(sys, "platform", "darwin"), \
         patch.object(backup.os, "replace", side_effect=OSError("publish failed")):
        try:
            with backup.pid_file(pid_path):
                pass
            failed = False
        except OSError:
            failed = True

    gc.collect()
    open_fds_after_failure = len(os.listdir(fd_root))
    cleaned_after_failure = not temporary_path.exists() and not pid_path.exists()
    with patch.object(sys, "platform", "darwin"):
        try:
            with backup.pid_file(pid_path):
                reacquired = True
        except RuntimeError:
            reacquired = False
    gc.collect()
    open_fds_after_reacquire = len(os.listdir(fd_root))

    check(
        "PID publication 실패는 temp/PID 파일 정리",
        failed and cleaned_after_failure,
    )
    check(
        "PID publication 실패 뒤 lock 재획득 및 FD 정리",
        reacquired
        and open_fds_after_failure == open_fds_before
        and open_fds_after_reacquire == open_fds_before,
        (
            f"(fd before={open_fds_before}, "
            f"failure={open_fds_after_failure}, "
            f"reacquire={open_fds_after_reacquire})"
        ),
    )


def test_backup_same_timestamp_rejected():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR / "collision_data"
    backup.BACKUP_DIR = _TMP_DIR / "collision_backups"
    backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
    attendance = SQLiteAttendanceRepository(
        backup.DATA_DIR / "attendance_data.db"
    )
    attendance.add_points(_TEST_GUILD, 1, 100)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
    fixed = datetime.datetime(2026, 7, 28, 12, tzinfo=datetime.timezone.utc)
    manifest = backup.create_backup_set(fixed)

    attendance.add_points(_TEST_GUILD, 2, 200)
    try:
        backup.create_backup_set(fixed)
        check("동일 시각 백업 충돌 거부", False)
    except RuntimeError:
        check("동일 시각 백업 충돌 거부", True)
    result = backup.verify_backup_set(manifest)
    check("충돌 후 기존 백업 보존", result["attendance_data.db"]["users"] == 1)


def test_prune_requires_timestamp_bound_filenames():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR / "retention_data"
    backup.BACKUP_DIR = _TMP_DIR / "retention_backups"
    backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
    backup.BACKUP_RETENTION_DAYS = 30
    SQLiteAttendanceRepository(
        backup.DATA_DIR / "attendance_data.db"
    ).add_points(_TEST_GUILD, 1, 100)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
    current = datetime.datetime(2026, 7, 20, tzinfo=datetime.timezone.utc)
    manifest = backup.create_backup_set(current)
    copied = backup.BACKUP_DIR / "20260101T000000Z-manifest.json"
    shutil.copy2(manifest, copied)

    now = datetime.datetime(2026, 7, 28, tzinfo=datetime.timezone.utc)
    deleted = backup.prune_backups(now)
    try:
        backup.verify_backup_set(manifest)
        preserved = True
    except RuntimeError:
        preserved = False
    check("manifest 시각과 다른 백업 파일은 정리하지 않음", deleted == 0)
    check("복사된 과거 manifest가 최신 백업을 보존", preserved)


def test_backup_publication_is_synced():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR / "durability_data"
    backup.BACKUP_DIR = _TMP_DIR / "durability_root" / "nested" / "backups"
    backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
    SQLiteAttendanceRepository(
        backup.DATA_DIR / "attendance_data.db"
    ).add_points(_TEST_GUILD, 1, 100)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
    events = []
    synced_directories = []
    real_fsync = backup.os.fsync
    real_replace = backup.os.replace
    real_fsync_directory = backup._fsync_directory

    def recording_fsync(descriptor):
        kind = "dir" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(f"fsync:{kind}")
        real_fsync(descriptor)

    def recording_replace(source, destination):
        real_replace(source, destination)
        name = pathlib.Path(destination).name
        events.append(
            "replace:manifest" if name.endswith("-manifest.json") else "replace:db"
        )

    def recording_fsync_directory(path):
        synced_directories.append(pathlib.Path(path))
        real_fsync_directory(path)

    backup.os.fsync = recording_fsync
    backup.os.replace = recording_replace
    backup._fsync_directory = recording_fsync_directory
    try:
        fixed = datetime.datetime(2026, 7, 28, 13, tzinfo=datetime.timezone.utc)
        backup.create_backup_set(fixed)
    finally:
        backup.os.fsync = real_fsync
        backup.os.replace = real_replace
        backup._fsync_directory = real_fsync_directory

    # DB 개수는 backup.DATABASES가 정한다. 개수를 테스트에 박아두지 않는다.
    db_count = len(backup.DATABASES)
    publication_events = events[-(2 * db_count + 4):]
    check(
        "DB 파일은 공개 전에 동기화",
        publication_events[: 2 * db_count]
        == ["fsync:file"] * db_count + ["replace:db"] * db_count,
        f"({publication_events})",
    )
    check(
        "DB rename은 manifest 공개 전에 디렉터리 동기화",
        publication_events[2 * db_count : 2 * db_count + 2] == ["fsync:dir", "fsync:file"],
    )
    check(
        "manifest rename 후 디렉터리 동기화",
        publication_events[2 * db_count + 2 :] == ["replace:manifest", "fsync:dir"],
    )
    check(
        "새 백업 디렉터리의 모든 상위 entry 동기화",
        {
            backup.BACKUP_DIR.parent,
            backup.BACKUP_DIR.parent.parent,
            _TMP_DIR,
        }.issubset(set(synced_directories)),
    )


def test_corrupt_backup_rejected():
    import module.backup as backup

    corrupt = _TMP_DIR / "corrupt.db"
    corrupt.write_bytes(b"not-a-sqlite-database")
    try:
        backup.verify_database(
            corrupt,
            {"users"},
            source_name="attendance_data.db",
        )
        check("손상 백업 거부", False)
    except RuntimeError:
        check("손상 백업 거부", True)


def test_malformed_manifest_rejected():
    import module.backup as backup

    malformed = _TMP_DIR / "malformed-manifest.json"
    malformed.write_text(
        '{"databases": [{"source": [], "backup": "backup.db"}]}',
        encoding="utf-8",
    )
    try:
        backup.verify_backup_set(malformed)
        check("잘못된 manifest 거부", False)
    except RuntimeError:
        check("잘못된 manifest 거부", True)


def test_prune_skips_invalid_utf8_manifest():
    import module.backup as backup

    backup.BACKUP_DIR = _TMP_DIR / "invalid-utf8-backups"
    backup.BACKUP_RETENTION_DAYS = 30
    backup.BACKUP_DIR.mkdir()
    expired = backup.BACKUP_DIR / "20260101T000000Z-manifest.json"
    current = backup.BACKUP_DIR / "20260728T000000Z-manifest.json"
    unrelated = backup.BACKUP_DIR / "keep-me.txt"
    expired.write_bytes(b"\xff")
    current.write_bytes(b"\xff")
    unrelated.write_text("keep", encoding="utf-8")

    try:
        deleted = backup.prune_backups(
            datetime.datetime(2026, 7, 28, tzinfo=datetime.timezone.utc)
        )
        skipped = deleted == 0
    except Exception:
        skipped = False

    check("잘못된 UTF-8 manifest 정리 중 건너뜀", skipped)
    check(
        "잘못된 UTF-8 manifest가 현재/무관 데이터 보존",
        expired.exists() and current.exists() and unrelated.exists(),
    )


if __name__ == "__main__":
    try:
        test_config_paths()
        test_config_validation()
        test_split_env_loading()
        test_public_env_contract()
        test_web_admin_atomic_settings_contract()
        test_forbidden_word_document_path()
        test_readme_public_distribution_contract()
        test_operations_document_contract()
        test_final_installation_and_operations_contract()
        test_deployment_contracts()
        test_macos_templates_render_portably()
        test_deployment_contracts_skip_only_compose_when_cli_missing()
        test_forbidden_words_degrade_gracefully()
        test_forbidden_words_load_logs_to_stdout()
        test_startup_syncs_commands_globally()
        test_startup_preverification_failure_stops_cogs_and_sync()
        test_startup_migrates_legacy_attendance_before_strict_verification()
        test_startup_cog_failure_stops_postverification_and_sync()
        test_instance_lock_rejects_second_holder()
        test_instance_lock_closes_failed_handle()
        test_main_holds_instance_lock_while_bot_runs()
        test_importing_main_does_not_construct_bot()
        test_bot_disables_all_mentions()
        test_sqlite_busy_timeout()
        test_backup_reads_wal_without_writer()
        test_point_ledger()
        test_luckybox_removed()
        test_guild_isolation()
        test_temp_image_lifecycle()
        repo = test_schema_initialization()
        test_schema_versions()
        test_deduct_points_atomicity(repo)
        test_ai_usage_atomicity()
        test_attendance_atomicity(repo)
        test_party_repository()
        test_party_capacity_constraint()
        test_party_cog_uses_epoch_seconds()
        test_persistent_party_panel_contract()
        test_factory()
        test_cog_facade()
        test_channel_sessions()
        test_imports()
        test_music_core_contract()
        test_backup_round_trip()
        test_settings_backup_round_trip()
        test_legacy_backup_restore_and_prune()
        test_invalid_backup_settings_prevent_creation()
        test_invalid_retention_prevents_pruning()
        test_invalid_interval_prevents_loop_entry()
        test_backup_loop_pid_lifecycle()
        test_pid_file_darwin_publishes_and_cleans()
        test_pid_file_linux_is_noop_without_artifacts()
        test_pid_file_rejects_contention_before_handoff()
        test_pid_file_replaces_stale_after_lock_release()
        test_pid_file_replace_occurs_while_companion_lock_is_held()
        test_pid_file_unlinks_before_companion_lock_release()
        test_pid_file_recovers_after_actual_abnormal_child_exit()
        test_pid_file_publication_failure_cleans_temp_lock_and_fds()
        test_backup_same_timestamp_rejected()
        test_prune_requires_timestamp_bound_filenames()
        test_backup_publication_is_synced()
        test_corrupt_backup_rejected()
        test_malformed_manifest_rejected()
        test_prune_skips_invalid_utf8_manifest()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # 임시 DB 정리

    print(f"\n{'='*40}\n결과: {PASS} 통과 / {FAIL} 실패")
    sys.exit(1 if FAIL else 0)
