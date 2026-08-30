# 콘솔 전용 테스트 (디스코드 연결 불필요)
# 실행: python -m test.console_tests (프로젝트 루트에서)
#
# 검증 항목:
#   1. SQLite 마이그레이션 (구버전 users 테이블에 luckybox 컬럼 추가)
#   4. PartyRepository CRUD (파티 생성/참가/탈퇴/만료 정리)
#   5. Repository 팩토리 (sqlite 선택, 미지원 백엔드 거부)
#   6. UsageCog 파사드 (Repository 주입 및 위임)
#   7. 채널별 대화 세션 분리 (히스토리 독립)
#   8. 전체 모듈 import 스모크 테스트
#
# 모든 테스트는 임시 디렉터리의 격리된 DB를 사용하므로 운영 데이터를 건드리지 않는다.

import asyncio
import datetime
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
from unittest.mock import patch

from dotenv import dotenv_values

# module.* import 전에 환경 변수를 설정해야 함
_TMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="hsr_test_"))
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ["DATA_DIR"] = str(_TMP_DIR)
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("GOOGLE_API_KEY", "test-dummy")

import module.database as database
from module.ai_chat_cog import AIChatCog
from module.database import (
    SQLiteGameUidRepository,
    SQLiteGuildSettingsRepository,
    SQLitePartyRepository,
    SQLiteUsageRepository,
    create_party_repository,
    create_usage_repository,
)
from module.usage_cog import UsageCog

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
            INSERT INTO users VALUES (7, 8, 9000, NULL, 3);
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
            "ADMIN_TOKEN",
        )
        if hasattr(config, name)
    }
    try:
        config.DISCORD_TOKEN = "test-token"
        config.OPENAI_API_KEY = None
        config.GOOGLE_API_KEY = None
        config.ADMIN_TOKEN = None
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
    (root / ".env.secrets").chmod(0o600)
    (root / ".env.runtime").chmod(0o600)

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
        "AI_USAGE_RETENTION_DAYS",
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
        "Docker Compose로 운영",
        "금지어 카운트, 파티, 길드 설정, 게임 UID 등록 데이터는 `guild_id`",
        "AI 사용량 한도만 사용자별·봇 인스턴스 전역",
        "`Administrator` 권한",
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
    check("AI 한도는 사용자별 인스턴스 전역", "사용자별·봇 인스턴스 전역" in operations)
    check("AI 한도는 KST 자정 리셋", "매일 KST 자정에 리셋" in operations)
    check("AI 한도는 명령별로 적용", "명령별로 적용" in operations)
    check("provider 계정 예산 안전망 유지", "OpenAI 계정 예산 한도" in operations)


def test_final_installation_and_operations_contract():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    operations = (PROJECT_ROOT / "docs/operations.md").read_text(encoding="utf-8")
    public_docs = f"{readme}\n{operations}"
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    quick_start = readme.split("## 빠른 시작", 1)[1].split("## 운영 시 주의", 1)[0]
    restore = operations.split("## 검증된 백업으로 실제 복구", 1)[1].split("## 배포", 1)[0]
    restore_script = restore.split("```bash", 1)[1].split("```", 1)[0]
    deployment = operations.split("## 배포", 1)[1].split("## 코드 롤백", 1)[0]
    docker_deploy = deployment.split("Docker:", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    rollback = operations.split("## 코드 롤백", 1)[1].split("## 호스트 한계", 1)[0]
    docker_rollback = rollback.split("Docker:", 1)[1].split("```bash", 1)[1].split("```", 1)[0]

    def ordered(text, *terms):
        cursor = -1
        for term in terms:
            cursor = text.find(term, cursor + 1)
            if cursor < 0:
                return False
        return True

    check(
        "설치 문서는 세 설정 JSON을 exact copy",
        """cp settings/persona.example.json settings/persona.json
cp settings/forbidden_words.example.json settings/forbidden_words.json
cp settings/games.example.json settings/games.json""" in quick_start,
    )
    check(
        "빠른 시작은 env 작성→copy→build→ownership→up→Guild 설정 순서",
        ordered(
            quick_start,
            "다음 단계로 가기 전에 값을 채웁니다.",
            "cp settings/persona.example.json settings/persona.json",
            "docker compose build bot",
            "BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)",
            "test -z \"$(sudo find settings runtime",
            "docker compose up -d",
            "Installation은 **Guild Install만**",
            "Discord에서 `/설정 시작`을 실행합니다.",
        ),
    )
    check(
        "Docker quick-start는 host test virtualenv를 build 전에 설치",
        """python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -r requirements-audit.txt
.venv/bin/python -m pip_audit -r requirements.lock
docker compose config --quiet
docker compose build bot""" in quick_start,
    )
    check(
        "설치 ownership은 image UID/GID와 restrictive mode를 exact 검증",
        r'''BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
sudo chown -R "$BOT_UID:$BOT_GID" settings runtime
sudo find settings runtime -type d -exec chmod 700 {} +
sudo find settings runtime -type f -exec chmod 600 {} +
test -z "$(sudo find settings runtime \( ! -uid "$BOT_UID" -o ! -gid "$BOT_GID" -o -perm -022 \) -print -quit)"''' in quick_start,
    )
    check(
        "설치는 container에서 settings rw와 runtime rw를 확인 후 up",
        ordered(
            quick_start,
            "docker compose run --rm --no-deps --entrypoint sh bot -c '",
            "test -r /app/settings/persona.json && test -w /app/settings/persona.json",
            "test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json",
            "test -r /app/settings/games.json && test -w /app/settings/games.json",
            "test -w /app/runtime/data && test -w /app/runtime/backups'",
            "docker compose run --rm --no-deps --entrypoint sh backup -c '",
            "test -r /app/settings/persona.json && test ! -w /app/settings/persona.json",
            "docker compose up -d",
        ),
    )
    check(
        "공지 opt-in은 Discord, 채널·필터는 localhost 웹 관리",
        "`/설정 공지허용`에서만 변경" in quick_start
        and "파티·공지·이벤트 채널과 금지어 필터 사용 여부는 봇 호스트 컴퓨터의 localhost 웹 관리" in quick_start,
    )
    check(
        "channel permission block 유지",
        "- `Manage Channels` — 봇 전용 category와 파티 채널 생성" in quick_start,
    )
    check(
        "voice permission은 더 이상 요구하지 않음",
        "`Connect`" not in quick_start and "`Speak`" not in quick_start,
    )
    check(
        "웹 관리는 선택 token·host loopback·unsupported remote 경계",
        "`ADMIN_TOKEN`은 선택 사항입니다." in readme
        and "host의 `127.0.0.1:8080`에만 publish" in readme
        and "다른 network interface에는 노출되지 않습니다." in readme
        and "원격 접근, reverse proxy, TLS, OAuth, 길드 관리자 웹 접근은 지원하지 않습니다." in readme
        and "원격 접근, reverse proxy, TLS, OAuth, 길드 관리자 웹 접근은 지원하지 않으며" in operations
        and "port가 아닌 host-scoped" in operations,
    )
    check(
        "공지는 opt-in Guild의 configured announcement channel만 대상",
        "웹 관리 공지는 Discord의 `/설정 공지허용`에서 opt-in한 Guild의 지정 공지 채널에만 보냅니다." in operations,
    )
    check(
        "GPL-3.0과 무기여 정책을 유지",
        "GNU General Public License v3.0" in readme
        and "사용자 기여를 받지 않습니다" in readme
        and "GNU GENERAL PUBLIC LICENSE" in (PROJECT_ROOT / "LICENSE").read_text(
            encoding="utf-8"
        ),
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
                "/설정 파티채널",
                "/설정 공지채널",
                "/설정 금지어",
            )
        ),
    )
    check(
        "백업은 네 DB와 존재한 세 settings를 exact same manifest로 설명",
        """각 backup set은 `usage_data.db`, `party_data.db`, `guild_settings.db`, `game_uid_data.db`와 당시 존재한 `settings/persona.json`, `settings/forbidden_words.json`, `settings/games.json`을 같은 manifest에 넣습니다.""" in operations,
    )
    check(
        "복구는 stop→stage→DB/settings→owner/mode→access→start→health/log",
        ordered(
            restore_script,
            "docker compose stop bot backup",
            "from module.backup import stage_restore",
            'cp -ip "$staged" "runtime/data/$name"',
            'cp -ip "$staged" "settings/$name"',
            "SERVICE_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)",
            'sudo chown -R "$SERVICE_UID:$SERVICE_GID" settings runtime',
            "sudo find settings runtime -type d -exec chmod 700 {} +",
            'test -z "$(sudo find settings runtime',
            "docker compose run --rm --no-deps --entrypoint sh bot -c '",
            "docker compose run --rm --no-deps --entrypoint sh backup -c '",
            "docker compose start backup",
            "docker compose start bot",
            "BOT_CONTAINER_ID=$(docker compose ps -q bot)",
            "docker inspect --format '{{.State.Health.Status}}' \"$BOT_CONTAINER_ID\"",
            "docker compose logs --tail=100 bot",
        )
        and "discordbot-hsr-bot-1" not in restore,
    )
    check(
        "복구는 Docker stage 뒤 operator에게 넘기고 restart 전 image user로 복귀",
        ordered(
            restore_script,
            "docker compose run --rm --no-deps -T --entrypoint python backup",
            'sudo chown -R "$(id -u):$(id -g)" settings runtime',
            'sudo chown -R "$SERVICE_UID:$SERVICE_GID" settings runtime',
        ),
    )
    trap_contract = (
        "trap restore_bot_mounts EXIT",
        "trap 'exit 129' HUP",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
    )
    mount_guard_contract = (
        "restore_bot_mounts() {",
        'sudo chown -R "$BOT_UID:$BOT_GID" settings runtime',
        "sudo find settings runtime -type d -exec chmod 700 {} +",
        "sudo find settings runtime -type f -exec chmod 600 {} +",
        "verify_bot_mounts() {",
        'test -z "$(sudo find settings runtime',
        "docker compose run --rm --no-deps --entrypoint sh bot -c '",
        "docker compose run --rm --no-deps --entrypoint sh backup -c '",
    )
    check(
        "Docker deploy는 stop→host handoff→Git→restore/verify→trap clear→restart",
        ordered(
            docker_deploy,
            "test -x .venv/bin/python",
            "docker compose stop bot backup",
            "BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)",
            "trap restore_bot_mounts EXIT",
            'sudo chown -R "$HOST_UID:$HOST_GID" settings',
            "git pull --ff-only",
            ".venv/bin/python -m test.console_tests",
            "docker compose build bot",
            "BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)",
            "restore_bot_mounts",
            "verify_bot_mounts",
            "trap - EXIT HUP INT TERM",
            "docker compose up -d --no-deps bot backup",
        )
        and all(term in docker_deploy for term in trap_contract + mount_guard_contract)
        and 'sudo chown -R "$HOST_UID:$HOST_GID" settings runtime' not in docker_deploy,
    )
    check(
        "Docker rollback은 stop→host handoff→Git→restore/verify→trap clear→restart",
        ordered(
            docker_rollback,
            "test -x .venv/bin/python",
            "docker compose stop bot backup",
            "BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)",
            "trap restore_bot_mounts EXIT",
            'sudo chown -R "$HOST_UID:$HOST_GID" settings',
            'revert) git revert "$TARGET_COMMIT"',
            ".venv/bin/python -m test.console_tests",
            "docker compose build bot",
            "BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)",
            "restore_bot_mounts",
            "verify_bot_mounts",
            "docker compose run --rm --no-deps backup python -m module.backup verify",
            "trap - EXIT HUP INT TERM",
            "docker compose up -d --no-deps bot backup",
        )
        and 'checkout) git checkout "$TARGET_COMMIT"' in docker_rollback
        and all(term in docker_rollback for term in trap_contract + mount_guard_contract)
        and 'sudo chown -R "$HOST_UID:$HOST_GID" settings runtime' not in docker_rollback,
    )
    check(
        "모든 Docker host venv workflow는 setup과 executable guard에 연결",
        "이 문서의 모든 `.venv/bin/python` 명령은 README 4단계에서 만든 host virtualenv를 전제로 합니다." in operations
        and ordered(docker_deploy, "test -x .venv/bin/python", ".venv/bin/python -m test.console_tests")
        and ordered(docker_rollback, "test -x .venv/bin/python", ".venv/bin/python -m test.console_tests"),
    )
    bot_service = compose.split("  bot:", 1)[1].split("\n  backup:", 1)[0]
    backup_service = compose.split("\n  backup:", 1)[1]
    check(
        "Compose service별 settings mode와 host loopback 전용 port 구조",
        "      - ./settings:/app/settings\n" in bot_service
        and "      - ./settings:/app/settings:ro\n" not in bot_service
        and "      - ./settings:/app/settings:ro\n" in backup_service
        # host 쪽 bind를 빠뜨리면 web admin이 모든 interface에 열린다.
        and '      - "127.0.0.1:8080:8080"\n' in bot_service
        # container 안에서는 publish 대상인 container interface에서 받아야 한다.
        and '      WEB_ADMIN_HOST: "0.0.0.0"\n' in bot_service
        and "ports:" not in backup_service,
    )
    expected_healthcheck = (
        "HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 "
        + chr(92)
        + "\n"
        + "    CMD [\"python\", \"-c\", \"import sys; sys.exit(0 if b'module.main' in open('/proc/1/cmdline', 'rb').read() else 1)\"]"
    )
    check(
        "Docker runtime은 apt 없이 main healthcheck",
        [
            line for line in dockerfile.splitlines() if "apt-get install" in line
        ] == []
        and "apt-get clean" not in dockerfile
        and expected_healthcheck in dockerfile
        and ordered(dockerfile, "USER bot", "HEALTHCHECK", 'CMD ["python", "-m", "module.main"]'),
    )


def test_requirements_lock_contract():
    source = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8")
    audioop_line = next(
        (line for line in lock.splitlines() if line.startswith("audioop-lts==")),
        "",
    )
    check(
        "lock 재생성 명령이 environment marker를 보존",
        "--universal --no-strip-markers" in source
        and "--universal --no-strip-markers" in lock,
    )
    check(
        "Python 3.13+ audioop 호환 패키지는 3.12 설치에서 제외",
        "python_version" in audioop_line
        and ">=" in audioop_line
        and "3.13" in audioop_line,
    )


def test_deployment_contracts():
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
            "bot 설정 마운트는 쓰기 가능",
            any(
                str(mount.get("source", "")).endswith("settings")
                and mount.get("target") == "/app/settings"
                and not mount.get("read_only", False)
                for mount in bot["volumes"]
            ),
        )
        check(
            "Compose web port는 host loopback에만 publish",
            not backup.get("ports")
            and [
                (port.get("host_ip"), str(port.get("published")), port.get("target"))
                for port in bot.get("ports", [])
            ]
            == [("127.0.0.1", "8080", 8080)],
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


def test_deployment_contracts_skip_compose_when_cli_missing():
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


def test_forbidden_words_degrade_gracefully():
    import module.config as config
    from module.forbidden_filter_cog import load_forbidden_words

    with tempfile.TemporaryDirectory() as directory:
        settings_dir = pathlib.Path(directory)
        with patch.object(config, "SETTINGS_DIR", settings_dir):
            check("금지어 파일 누락 시 필터 비활성", load_forbidden_words() == [])
            (settings_dir / "forbidden_words.json").write_text("{}", encoding="utf-8")
            check("금지어 JSON 구조 오류 시 필터 비활성", load_forbidden_words() == [])


def test_forbidden_words_load_logs_to_stdout():
    import module.config as config
    import module.forbidden_filter_cog as forbidden_filter_cog

    with tempfile.TemporaryDirectory() as directory:
        pathlib.Path(directory, "forbidden_words.json").write_text(
            '["금지어"]', encoding="utf-8"
        )
        output = StringIO()
        with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)), redirect_stdout(output):
            forbidden_filter_cog.ForbiddenFilterCog(bot=None)

    check(
        "금지어 로드 로그가 stdout에 기록",
        output.getvalue().strip() == "📥 금지어 1개 로드 (허용 0개)",
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
        def get_cog(self, name):
            # 실제 commands.Bot에는 항상 있다. setup_hook의 cog 등록 자기 점검용.
            return object()

        tree = FakeTree()

        def add_view(self, view):
            events.append("view:setup")

        async def add_cog(self, cog):
            events.append("cog:guild_settings")

        async def load_extension(self, extension):
            events.append(f"load:{extension}")
            if extension == "module.guild_settings_cog":
                from module.guild_settings_cog import setup

                await setup(self)

    def verify(*, existing_only=False, allow_legacy=False):
        events.append("verify:pre" if existing_only and allow_legacy else "verify:post")

    with patch.object(main, "DATA_DIR", data_dir), \
         patch.object(main, "_verify_databases", side_effect=verify):
        asyncio.run(main.HyacineBot.setup_hook(FakeBot()))

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
        events.index("verify:pre") < events.index("load:module.guild_settings_cog")
        < events.index("verify:post")
        < events.index("sync:global"),
        f"({events})",
    )
    check(
        "길드 설정 Cog가 로드 목록에 포함",
        "load:module.guild_settings_cog" in events,
        f"({events})",
    )
    check(
        "길드 고정 잔재 없음",
        "copy_global_to" not in inspect.getsource(main.HyacineBot.setup_hook)
        and "interaction_check" not in inspect.getsource(main.HyacineBot.setup_hook),
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
            asyncio.run(main.HyacineBot.setup_hook(FakeBot()))
            check("사전 DB 검증 실패 전파", False)
        except RuntimeError:
            check("사전 DB 검증 실패 전파", True)
    check("사전 DB 검증 실패 시 Cog와 sync 미실행", events == ["verify:usage_data.db"], f"({events})")


def test_startup_migrates_legacy_attendance_before_strict_verification():
    import module.main as main

    data_dir = _TMP_DIR / "legacy-startup-data"
    attendance_path = data_dir / "usage_data.db"
    _create_legacy_attendance_db(attendance_path)
    SQLitePartyRepository(data_dir / "party_data.db")
    SQLiteGuildSettingsRepository(data_dir / "guild_settings.db")
    events = []

    class FakeBot:
        def get_cog(self, name):
            # 실제 commands.Bot에는 항상 있다. setup_hook의 cog 등록 자기 점검용.
            return object()

        class tree:
            @staticmethod
            async def sync():
                events.append("sync")

        async def load_extension(self, extension):
            events.append(f"load:{extension}")
            if extension == "module.usage_cog":
                SQLiteUsageRepository(attendance_path)
                events.append("migrate:attendance")
            if extension == "module.game_profile_cog":
                SQLiteGameUidRepository(data_dir / "game_uid_data.db")

    with patch.object(main, "DATA_DIR", data_dir):
        try:
            asyncio.run(main.HyacineBot.setup_hook(FakeBot()))
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
        forbidden = conn.execute(
            "SELECT forbidden_count FROM users WHERE guild_id = 7 AND user_id = 8"
        ).fetchone()
        user_columns = {row[1] for row in conn.execute('PRAGMA table_info("users")')}
    check("legacy attendance startup reaches repository migration", started)
    check(
        "startup migration precedes sync and preserves data",
        version == 4
        and "ai_usage" in tables
        and "point_ledger" not in tables
        and not {"points", "last_attendance_date"} & user_columns
        and forbidden == (3,)
        and "migrate:attendance" in events
        and events.index("migrate:attendance") < events.index("sync"),
        f"(version={version}, events={events})",
    )


def test_startup_requires_cross_cog_dependencies():
    """다른 cog가 참조하는 cog가 빠지면 조용한 None이 아니라 기동 실패여야 한다."""
    import module.main as main

    events = []

    class FakeBot:
        class tree:
            @staticmethod
            async def sync():
                events.append("sync")

        def __init__(self, missing):
            self.missing = missing

        def get_cog(self, name):
            return None if name == self.missing else object()

        async def load_extension(self, extension):
            events.append(f"load:{extension}")

    for dependency in main.CROSS_COG_DEPENDENCIES:
        events.clear()
        with patch.object(main, "_verify_databases"):
            try:
                asyncio.run(
                    main.HyacineBot.setup_hook(FakeBot(dependency.__name__))
                )
                raised = ""
            except RuntimeError as error:
                raised = str(error)
        check(
            f"{dependency.__name__} 미등록 시 기동 실패",
            dependency.__name__ in raised,
            f"(raised={raised!r})",
        )
        check(
            f"{dependency.__name__} 미등록 시 sync 미실행",
            "sync" not in events,
            f"({events})",
        )

    events.clear()
    with patch.object(main, "_verify_databases"):
        asyncio.run(main.HyacineBot.setup_hook(FakeBot(missing=None)))
    check("의존 cog가 모두 있으면 sync까지 진행", "sync" in events, f"({events})")


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

    with patch.object(main, "available_extensions", return_value=[
             main.EXTENSIONS[0][0], main.EXTENSIONS[1][0]
         ]), \
         patch.object(pathlib.Path, "exists", return_value=True), \
         patch.object(main, "verify_database", side_effect=verify):
        try:
            asyncio.run(main.HyacineBot.setup_hook(FakeBot()))
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
    data_dir = _TMP_DIR / "main-lock-data"
    backup_dir = _TMP_DIR / "main-lock-backups"
    data_dir.mkdir(exist_ok=True)
    backup_dir.mkdir(exist_ok=True)

    class FakeLock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, exc_type, exc, traceback):
            events.append("unlock")

    class FakeBot:
        def run(self, token):
            events.append("run")

    def acquire(path):
        events.append(f"acquire:{path.name}")
        return FakeLock()

    with patch.object(main, "validate_config"), \
         patch.object(main, "DATA_DIR", data_dir), \
         patch.object(main, "BACKUP_DIR", backup_dir), \
         patch.object(main, "acquire_instance_lock", side_effect=acquire), \
         patch.object(main, "HyacineBot", FakeBot):
        main.main()

    check(
        "봇 실행 수명 동안 instance lock 유지",
        events
        == [
            "acquire:.bot.lock",
            "lock",
            "run",
            "unlock",
        ],
        f"({events})",
    )


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
    from discord.ext import commands

    import module.main as main

    with patch.object(commands.Bot, "__init__", return_value=None) as init:
        main.HyacineBot()

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


def test_sqlite_connect_tolerates_vanishing_wal_sidecar():
    """-wal/-shm은 마지막 연결이 닫힐 때 사라진다. 그 순간 다른 연결이 열려도
    권한 조이기가 FileNotFoundError로 터지면 안 된다. 동시 run_db 두 개가
    같은 DB를 열 때 실제로 나던 경합이다."""
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "race.db"
        with closing(database._connect(path)) as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")

        wal = path.with_name(f"{path.name}-wal")
        real_lstat = pathlib.Path.lstat

        def vanishing_lstat(self):
            # exists() 확인 직후 사라지는 sidecar를 흉내낸다.
            if self.name == wal.name:
                raise FileNotFoundError(2, "No such file or directory", str(self))
            return real_lstat(self)

        with patch.object(pathlib.Path, "lstat", vanishing_lstat):
            try:
                with closing(database._connect(path)) as conn:
                    conn.execute("SELECT 1").fetchone()
                survived = True
            except FileNotFoundError:
                survived = False
        check("사라진 WAL sidecar에도 연결 성공", survived)

        # symlink 거부는 그대로여야 한다.
        link_target = pathlib.Path(directory) / "elsewhere.db"
        link_target.touch()
        linked = pathlib.Path(directory) / "linked.db"
        linked.symlink_to(link_target)
        try:
            database._connect(linked)
            rejected = False
        except PermissionError:
            rejected = True
        check("symlink DB 경로는 여전히 거부", rejected)


def test_backup_reads_wal_without_writer():
    import module.backup as backup

    with tempfile.TemporaryDirectory() as directory:
        data_dir = pathlib.Path(directory) / "data"
        data_dir.mkdir()
        source = data_dir / "usage_data.db"
        repo = SQLiteUsageRepository(source)
        repo.increment_forbidden_count(_TEST_GUILD, 1)
        del repo  # 쓰기 연결 없음 = 봇 정지 상태
        gc.collect()

        with closing(sqlite3.connect(source)) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        check("WAL 모드로 저장됨", mode == "wal", f"({mode})")

        target = pathlib.Path(directory) / "copy.db"
        backup._backup_one(source, target)
        with closing(sqlite3.connect(target)) as conn:
            counted = conn.execute(
                "SELECT forbidden_count FROM users WHERE user_id = 1"
            ).fetchone()
        check("쓰기 프로세스 없이도 백업 가능", counted == (1,), f"({counted})")


def test_guild_isolation():
    print("\n[0] 길드 격리")
    import module.database as database
    import module.forbidden_filter_cog as forbidden_filter_cog
    import module.party_cog as party_cog

    data_dir = _TMP_DIR / "guild-isolation"
    data_dir.mkdir(exist_ok=True)
    usage = database.SQLiteUsageRepository(data_dir / "a.db")
    parties = database.SQLitePartyRepository(data_dir / "p.db")
    settings = database.SQLiteGuildSettingsRepository(data_dir / "s.db")
    profiles = database.SQLiteGameUidRepository(data_dir / "g.db")

    A, B, USER = 1001, 1002, 7
    # 같은 사람이 서버마다 별도 카운트를 갖는다. 경계는 코드가 아니라 스키마가 만든다.
    usage.increment_forbidden_count(A, USER)
    usage.increment_forbidden_count(A, USER)
    usage.increment_forbidden_count(B, USER)
    check(
        "금지어 카운트는 서버별로 분리",
        (usage.get_forbidden_count(A, USER), usage.get_forbidden_count(B, USER)) == (2, 1),
    )

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

    # 두 번째 길드에 초대된 상황. 같은 사람이 서버마다 다른 계정을 등록하고,
    # 한쪽에서만 금지어 필터를 꺼도 다른 쪽 동작은 그대로여야 한다.
    settings.set_party_channel(B, 444)
    settings.set_forbidden_filter_enabled(B, False)
    profiles.set_uid(A, USER, "hsr", "800000001")
    profiles.set_uid(B, USER, "hsr", "800000002")
    profiles.set_uid(B, USER, "gi", "900000002")
    check(
        "두 번째 길드 설정·등록은 첫 길드와 독립",
        settings.get_forbidden_filter_enabled(A) is True
        and settings.get_forbidden_filter_enabled(B) is False
        and settings.get_party_channel(A) == 333
        and settings.get_party_channel(B) == 444
        and profiles.get_uid(A, USER, "hsr") == "800000001"
        and profiles.list_uids(B, USER) == {"hsr": "800000002", "gi": "900000002"},
    )

    # 봇이 서버에서 제거되면 그 서버 것만 지운다.
    for repo in (usage, parties, settings, profiles):
        repo.delete_guild(A)
    check("제거된 서버 데이터 삭제", usage.get_forbidden_count(A, USER) == 0 and parties.get_party(A, "PUBG") is None
          and settings.get_party_channel(A) is None and profiles.list_uids(A, USER) == {})
    check("다른 서버는 보존", usage.get_forbidden_count(B, USER) == 1 and parties.get_party(B, "PUBG") is not None
          and profiles.get_uid(B, USER, "hsr") == "800000002")

    check(
        "이탈 후에도 두 번째 길드 설정 보존",
        settings.get_party_channel(B) == 444
        and settings.get_forbidden_filter_enabled(B) is False,
    )

    # 격리는 코드가 아니라 스키마가 만든다. 모든 테이블의 PK에 guild_id가 있어야 한다.
    for repository in (usage, parties, settings, profiles):
        with sqlite3.connect(repository.db_path) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
                )
            ]
            for table in tables:
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                primary_key = {
                    row[1] for row in conn.execute(f'PRAGMA table_info("{table}")') if row[5]
                }
                # ai_usage는 인스턴스 전역이라는 명시적 예외다.
                if table == "ai_usage":
                    continue
                check(
                    f"{repository.db_path.name}:{table} 기본키에 guild_id",
                    "guild_id" in primary_key,
                    f"({sorted(columns)})",
                )

    check(
        "DM은 금지어 집계에서 제외",
        "message.guild is None"
        in inspect.getsource(
            forbidden_filter_cog.ForbiddenFilterCog._inspect_message
        ),
    )
    check(
        "파티 패널은 길드 밖 상호작용을 거부",
        "guild_id is None"
        in inspect.getsource(party_cog.PartyCog._reject_invalid_interaction),
    )


def test_temp_image_lifecycle():
    print("\n[0] 임시 이미지 경로와 수명")
    import module.ai_image_cog as ai_image_cog
    import module.config as config

    cog = object.__new__(ai_image_cog.AIImageCog)
    cog.temporary_image_directory = config.DATA_DIR / "temp_images"
    cog.temporary_image_directory.mkdir(parents=True, exist_ok=True)
    check("임시 경로는 DATA_DIR 아래", cog.temporary_image_directory.is_relative_to(config.DATA_DIR))

    stale = cog.temporary_image_directory / "stale.png"
    fresh = cog.temporary_image_directory / "fresh.png"
    stale.write_bytes(b"png")
    fresh.write_bytes(b"png")
    old = time.time() - ai_image_cog.TEMP_IMAGE_TTL_SECONDS - 60
    os.utime(stale, (old, old))

    cog._sweep_stale_images()

    check("시작 시 오래된 임시 파일 정리", not stale.exists())
    check("최근 파일은 보존", fresh.exists())
    fresh.unlink()


def test_schema_initialization() -> SQLiteUsageRepository:
    print("\n[1] SQLite 스키마 초기화")
    db_path = _TMP_DIR / "attendance_schema.db"

    repo = SQLiteUsageRepository(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        pk = [row[1] for row in conn.execute("PRAGMA table_info(users)") if row[5]]
        ai_cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_usage)")}
        ai_pk = [row[1] for row in conn.execute("PRAGMA table_info(ai_usage)") if row[5]]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    check("users 컬럼 구성", cols == {"guild_id", "user_id", "forbidden_count", "bio"}, f"({cols})")
    check("복합 기본키 (guild_id, user_id)", pk == ["guild_id", "user_id"], f"({pk})")
    check("원장 테이블 없음", "point_ledger" not in tables)
    check("AI 사용량 테이블 생성", "ai_usage" in tables)
    check("AI 사용량은 guild_id 없이 전역", ai_cols == {"user_id", "usage_date", "command", "count"})
    check("AI 사용량 복합 기본키", ai_pk == ["user_id", "usage_date", "command"])
    check("폐기된 luckybox 컬럼 없음", not {"luckybox_count", "last_luckybox_date"} & cols)

    # 중복 실행해도 에러가 없어야 함 (멱등성)
    SQLiteUsageRepository(db_path)
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
    SQLiteUsageRepository(attendance_path)
    SQLitePartyRepository(party_path)
    SQLiteGuildSettingsRepository(settings_path)

    check("usage 스키마 버전", _user_version(attendance_path) == 4)
    check("party 스키마 버전", _user_version(party_path) == 2)
    check("settings 스키마 버전", _user_version(settings_path) == 6)

    for label, repository in (
        ("attendance", SQLiteUsageRepository),
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
        conn.execute(
            "INSERT INTO users (guild_id, user_id, forbidden_count) "
            "VALUES (1, 2, 4)"
        )
    SQLiteUsageRepository(attendance_path)
    check(
        "무버전 usage 데이터 보존",
        SQLiteUsageRepository(attendance_path).get_forbidden_count(1, 2) == 4,
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
            INSERT INTO users VALUES (7, 8, 9000, NULL, 5);
            PRAGMA user_version = 1;
        """)
    migrated = SQLiteUsageRepository(version_one_path)
    check("usage v1에서 v4로 마이그레이션", _user_version(version_one_path) == 4)
    check("usage v1 금지어 카운트 보존", migrated.get_forbidden_count(7, 8) == 5)
    check("usage v1 마이그레이션 후 자기소개는 비어 있음", migrated.get_bio(7, 8) is None)

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
        legacy_path = _TMP_DIR / f"settings_v{legacy_version}_to_v6.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, recruit_channel_id INTEGER, event_channel_id INTEGER)"
            )
            conn.execute("INSERT INTO guild_settings VALUES (7, 700, 701)")
            conn.execute(f"PRAGMA user_version = {legacy_version}")
        SQLiteGuildSettingsRepository(legacy_path)
        with sqlite3.connect(legacy_path) as conn:
            row = conn.execute(
                "SELECT party_channel_id, allow_host_announce, event_channel_id "
                "FROM guild_settings WHERE guild_id = 7"
            ).fetchone()
            forbidden_default = conn.execute(
                "SELECT forbidden_filter_enabled FROM guild_settings WHERE guild_id = 7"
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
        check(
            f"settings v{legacy_version} 채널 보존",
            row == (700, 0, 701),
        )
        check(
            f"settings v{legacy_version} 금지어 필터 기본 켜짐",
            forbidden_default == (1,),
        )
        check(f"settings v{legacy_version} party_panels 생성", "party_panels" in tables)
        check(
            f"settings v{legacy_version} 정확한 패널 스키마",
            guild_columns == (
                "guild_id",
                "party_channel_id",
                "allow_host_announce",
                "forbidden_filter_enabled",
                "announcement_channel_id",
                "event_channel_id",
            )
            and panel_columns == ("guild_id", "game", "message_id")
            and panel_primary_key == ("guild_id", "game"),
        )
        check(f"settings v{legacy_version} 버전", version == 6)

    # v2(음악 컬럼 보유), v3(토글 없음), v4(공지 채널 없음), v5(이벤트 채널 없음)가 v6로
    # 올라온다. 기존 토글 값은 그대로여야 한다.
    settings_upgrade_schemas = {
        2: """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                music_channel_id INTEGER,
                music_panel_msg_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO guild_settings VALUES (7, 700, 800, 801, 1);
        """,
        3: """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE party_panels (
                guild_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, game)
            );
            INSERT INTO guild_settings VALUES (7, 700, 1);
            INSERT INTO party_panels VALUES (7, 'LOL', 900);
        """,
        4: """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0,
                forbidden_filter_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE party_panels (
                guild_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, game)
            );
            INSERT INTO guild_settings VALUES (7, 700, 1, 0);
            INSERT INTO party_panels VALUES (7, 'LOL', 900);
        """,
        5: """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0,
                forbidden_filter_enabled INTEGER NOT NULL DEFAULT 1,
                announcement_channel_id INTEGER
            );
            CREATE TABLE party_panels (
                guild_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, game)
            );
            INSERT INTO guild_settings VALUES (7, 700, 1, 1, 800);
            INSERT INTO party_panels VALUES (7, 'LOL', 900);
        """,
    }
    for old_version, schema in settings_upgrade_schemas.items():
        upgrade_path = _TMP_DIR / f"settings_v{old_version}_to_v6.db"
        with sqlite3.connect(upgrade_path) as conn:
            conn.executescript(schema)
            conn.execute(f"PRAGMA user_version = {old_version}")
        repository = SQLiteGuildSettingsRepository(upgrade_path)
        with sqlite3.connect(upgrade_path) as conn:
            columns = tuple(
                row[1] for row in conn.execute("PRAGMA table_info(guild_settings)")
            )
        check(
            f"settings v{old_version}에서 v6로 마이그레이션",
            _user_version(upgrade_path) == 6
            and columns == (
                "guild_id",
                "party_channel_id",
                "allow_host_announce",
                "forbidden_filter_enabled",
                "announcement_channel_id",
                "event_channel_id",
            ),
            f"({_user_version(upgrade_path)}, {columns})",
        )
        check(
            f"settings v{old_version} 기존 설정 보존",
            repository.get_party_channel(7) == 700
            and repository.get_allow_host_announce(7) is True,
        )
        check(
            f"settings v{old_version} 금지어 필터는 켜진 채로 넘어옴",
            repository.get_forbidden_filter_enabled(7) == (old_version != 4),
        )

    # 미등록 길드도 켜짐이 기본이다. 행이 없다고 필터가 꺼지면 안 된다.
    toggle = SQLiteGuildSettingsRepository(_TMP_DIR / "settings_toggle.db")
    check("미등록 길드 금지어 필터 기본 켜짐", toggle.get_forbidden_filter_enabled(11))
    toggle.set_forbidden_filter_enabled(11, False)
    check("금지어 필터 끄기 반영", toggle.get_forbidden_filter_enabled(11) is False)
    toggle.set_forbidden_filter_enabled(11, True)
    check("금지어 필터 다시 켜기 반영", toggle.get_forbidden_filter_enabled(11) is True)
    check(
        "금지어 필터 토글은 다른 길드에 번지지 않음",
        toggle.get_forbidden_filter_enabled(12),
    )

    malformed_schemas = {
        "extra guild_settings column": """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0,
                forbidden_filter_enabled INTEGER NOT NULL DEFAULT 1,
                obsolete INTEGER
            )
        """,
        "wrong party_panels primary key": """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0,
                forbidden_filter_enabled INTEGER NOT NULL DEFAULT 1
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
    settings.set_announcement_channel(7, 701)
    settings.set_event_channel(7, 702)
    settings.set_allow_host_announce(7, True)
    settings.set_allow_host_announce(8, True)
    settings.clear_channel(7, 703)
    check("party panel upsert/list/delete", panels_after_upsert == {"LOL": 71, "PUBG": 72} and settings.get_party_panels(7) == {"PUBG": 72})
    check("무관한 채널 삭제는 채널 설정을 건드리지 않음", settings.get_party_channel(7) == 700 and settings.get_announcement_channel(7) == 701 and settings.get_event_channel(7) == 702)
    settings.clear_channel(7, 702)
    check("삭제된 이벤트 채널은 해제", settings.get_event_channel(7) is None)
    settings.clear_channel(7, 701)
    check("삭제된 공지 채널은 해제", settings.get_announcement_channel(7) is None)
    settings.clear_channel(7, 700)
    check("삭제된 파티 채널은 해제", settings.get_party_channel(7) is None)
    check("공지 허용 길드 목록", settings.get_allow_host_announce(7) and settings.list_announcement_guild_ids() == [7, 8])
    settings.delete_guild(7)
    check("길드 삭제는 설정과 party panel 정리", settings.get_party_panels(7) == {} and settings.get_party_channel(7) is None)


def test_ai_usage_atomicity():
    print("\n[2] AI 일일 사용량 원자성")
    repo = SQLiteUsageRepository(_TMP_DIR / "ai_usage_atomicity.db")
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
    from module.party_cog import PartyCog

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
    cog = object.__new__(PartyCog)
    cog.party_repository = repository
    with patch("time.time", return_value=2_000_000_000.75):
        asyncio.run(cog.create_party(_TEST_GUILD, "LOL"))
        asyncio.run(PartyCog.cleanup_parties.coro(cog))

    check("Cog 파티 생성 시 epoch 정수 전달", repository.created_at == 2_000_000_000)
    check(
        "Cog 만료 정리 시 24시간 전 epoch 정수 전달",
        repository.cutoff == 1_999_913_600,
    )


def test_persistent_party_panel_contract():
    import module.party_cog as party_cog

    source = inspect.getsource(party_cog)
    check(
        "구 파티 slash command 제거",
        all(f'name="{name}"' not in source for name in ("모집", "파티", "나가기", "변경")),
    )
    check(
        "파티 패널 custom_id는 SHA-256 digest 사용",
        "hashlib.sha256" in source and "_game_component_key(game)" in source,
    )
    check(
        "파티 패널은 startup/setup/cleanup 복구 경로 제공",
        all(name in party_cog.PartyCog.__dict__ for name in (
            "ensure_panels", "render_game_panel", "on_ready", "on_member_remove"
        )),
    )


def test_factory():
    print("\n[5] Repository 팩토리")
    # sqlite 백엔드 (기본값)
    a_repo = create_usage_repository()
    p_repo = create_party_repository()
    check("sqlite 백엔드: UsageRepository 생성", isinstance(a_repo, SQLiteUsageRepository))
    check("sqlite 백엔드: PartyRepository 생성", isinstance(p_repo, SQLitePartyRepository))
    check("DB 파일이 DATA_DIR 아래에 생성됨", a_repo.db_path.parent == _TMP_DIR.resolve())

    # 미지원 백엔드는 명확한 에러를 내야 함
    original = database.DB_BACKEND
    database.DB_BACKEND = "oracle"
    try:
        try:
            create_usage_repository()
            check("미지원 백엔드 거부 (NotImplementedError)", False)
        except NotImplementedError:
            check("미지원 백엔드 거부 (NotImplementedError)", True)
    finally:
        database.DB_BACKEND = original


def test_cog_facade():
    print("\n[6] UsageCog 파사드 (Repository 주입)")
    raw = SQLiteUsageRepository(_TMP_DIR / "facade_test.db")
    cog = UsageCog(bot=None, usage_repository=raw)
    G = _TEST_GUILD

    # 파사드는 모두 async다. 동기 리포지토리를 스레드로 넘겨 이벤트 루프를 지킨다.
    asyncio.run(cog.increment_forbidden_count(G, 42))
    asyncio.run(cog.increment_forbidden_count(G, 42))
    check("forbidden_count 위임", asyncio.run(cog.get_forbidden_count(G, 42)) == 2)
    kst_now = datetime.datetime(2026, 8, 4, 23, 59, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
    with patch("module.usage_cog.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = kst_now
        reservation = asyncio.run(cog.reserve_ai_usage(42, "light", 3))
        check("KST 날짜로 AI 사용량 예약", reservation == ("2026-08-04", 1))
        check("AI 사용량 조회 위임", asyncio.run(cog.get_ai_usage(42, "light")) == 1)
        check("AI 사용량 반환 위임", asyncio.run(cog.release_ai_usage(42, "2026-08-04", "light")) is True)
    check(
        "파사드 전체가 코루틴",
        all(
            inspect.iscoroutinefunction(getattr(UsageCog, name))
            for name in ("reserve_ai_usage", "release_ai_usage",
                         "get_ai_usage", "increment_forbidden_count", "get_forbidden_count")
        ),
    )


def test_channel_sessions():
    print("\n[7] 채널별 대화 세션 분리")
    cog = AIChatCog(bot=None)

    s1 = cog.get_or_create_session(111)
    s2 = cog.get_or_create_session(222)

    check("두 채널은 서로 다른 세션 객체", s1 is not s2)
    check("같은 채널은 같은 세션 재사용", cog.get_or_create_session(111) is s1)
    # 히스토리 분리
    s1.history.append({"role": "user", "content": "채널 111의 비밀 이야기"})
    s1.history.append({"role": "assistant", "content": "네, 기억할게요"})
    cog._trim_history(s1)

    s2_contents = [m["content"] for m in s2.history if m["role"] != "system"]
    check("채널 111의 대화가 채널 222에 노출되지 않음", len(s2_contents) == 0)
    s1_contents = [m["content"] for m in s1.history if m["role"] != "system"]
    check("채널 111 히스토리는 유지됨", "채널 111의 비밀 이야기" in s1_contents)

    # trim이 system 프롬프트를 보존하는지
    check("trim 후 system 프롬프트 보존", any(m["role"] == "system" for m in s1.history))

    lru_cog = AIChatCog(bot=None)
    for channel_id in range(lru_cog.MAX_CHANNEL_SESSIONS):
        lru_cog.get_or_create_session(channel_id)
    lru_cog.get_or_create_session(0)
    lru_cog.get_or_create_session(lru_cog.MAX_CHANNEL_SESSIONS)
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
        "module.panel",
        "module.guild_settings_cog",
        "module.usage_cog",
        "module.party_cog",
        "module.scheduled_event_cog",
        "module.forbidden_filter_cog",
        "module.greeting_cog",
        "module.enka_profiles",
        "module.game_profile_cog",
        "module.web_admin_cog",
        "module.ai_chat_cog",
        "module.ai_image_cog",
    ]
    for m in mods:
        try:
            importlib.import_module(m)
            check(f"import {m}", True)
        except Exception as e:
            check(f"import {m}", False, f"({e})")


def test_repository_layer_is_the_only_sql_surface():
    """SQL이 저장소 계층 밖으로 새지 않는지 AST로 고정한다.

    §8.4의 추상화는 이미 구현돼 있다. 이 테스트는 새로 만드는 게 아니라
    깨지지 않게 못을 박는 것이다. 외부 DB로 교체할 때 고쳐야 할 파일이
    database.py 하나로 유지된다는 뜻이기도 하다.
    """
    print("\n[9] 저장소 계층 밖 sqlite3 직접 사용 금지")
    import ast

    # database.py는 저장소 구현 본체. backup.py는 파일 단위 스냅샷이라 SQL이 아닌
    # sqlite3 백업 API를 쓴다. export_legacy.py는 삭제된 컬럼을 읽는 일회성 도구로,
    # 현재 스키마를 모르는 구버전 DB를 상대하므로 저장소를 경유할 수 없다.
    allowed = {"database.py", "backup.py", "export_legacy.py"}

    for path in sorted((PROJECT_ROOT / "module").glob("*.py")):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.update(
                    alias.name for alias in node.names if alias.name.split(".")[0] == "sqlite3"
                )
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlite3":
                offenders.add(node.module)
        check(f"{path.name}는 sqlite3를 import하지 않음", not offenders, f"({sorted(offenders)})")

    # 예외 목록이 실재하는 파일만 담고 있어야 한다. 파일이 사라지면 구멍이 남는다.
    check(
        "예외 목록의 파일이 모두 존재",
        all((PROJECT_ROOT / "module" / name).exists() for name in allowed),
    )


def test_backup_round_trip():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR
    backup.BACKUP_DIR = _TMP_DIR / "backups"
    backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
    SQLiteUsageRepository(_TMP_DIR / "usage_data.db").increment_forbidden_count(_TEST_GUILD, 77)
    SQLitePartyRepository(_TMP_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(_TMP_DIR / "guild_settings.db")
    SQLiteGameUidRepository(_TMP_DIR / "game_uid_data.db").set_uid(
        _TEST_GUILD, 77, "hsr", "800333171"
    )

    manifest = backup.create_backup_set()
    result = backup.verify_backup_set(manifest)
    check("출석 DB 백업 검증", result["usage_data.db"]["users"] == 1)
    check("파티 DB 백업 검증", result["party_data.db"]["parties"] == 1)
    check("프로필 DB 백업 검증", result["game_uid_data.db"]["game_uids"] == 1)
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
        SQLiteUsageRepository(data_dir / "usage_data.db")
        SQLitePartyRepository(data_dir / "party_data.db")
        SQLiteGuildSettingsRepository(data_dir / "guild_settings.db")
        SQLiteGameUidRepository(data_dir / "game_uid_data.db")
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


def test_setting_source_inspection_failure_closes_descriptor():
    import module.backup as backup

    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        source = root / "persona.json"
        source.write_text("{}", encoding="utf-8")
        opened = []
        real_open = backup.os.open

        def recording_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with patch.object(backup.os, "open", side_effect=recording_open), patch.object(
            backup.os, "fstat", side_effect=OSError("inspection failed")
        ):
            try:
                backup._copy_setting(source, root / "copy.json")
                failure = None
            except RuntimeError as exc:
                failure = exc

        try:
            os.fstat(opened[0])
            closed = False
        except OSError:
            closed = True

    check(
        "설정 source 검사 실패를 RuntimeError로 연결",
        failure is not None and isinstance(failure.__cause__, OSError),
    )
    check("설정 source 검사 실패 시 descriptor 종료", closed)


def test_legacy_backup_restore_and_prune():
    import module.backup as backup

    root = _TMP_DIR / "legacy-backup-lifecycle"
    data_dir = root / "data"
    backup_dir = root / "backups"
    backup.DATA_DIR = data_dir
    backup.BACKUP_DIR = backup_dir
    backup.BACKUP_RETENTION_DAYS = 30
    timestamp = "20260101T000000Z"
    _create_legacy_attendance_db(data_dir / "usage_data.db")
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
        conn.commit()
    with closing(sqlite3.connect(party_path)) as conn:
        conn.execute("PRAGMA user_version = 0")
    # game_uid_data.db는 구버전이 없다. 현재 스키마 그대로 함께 백업된다.
    SQLiteGameUidRepository(data_dir / "game_uid_data.db").set_uid(
        _TEST_GUILD, 8, "hsr", "800333171"
    )
    backup_dir.mkdir(parents=True)

    items = []
    for source_name, current_tables in backup.DATABASES.items():
        source = data_dir / source_name
        copied = backup_dir / f"{timestamp}-{source_name}"
        backup._backup_one(source, copied)
        tables = (
            {"users", "point_ledger"}
            if source_name == "usage_data.db"
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
        and verified["usage_data.db"] == {"point_ledger": 1, "users": 1},
    )
    legacy_v0 = root / "legacy-v0-attendance.db"
    shutil.copy2(data_dir / "usage_data.db", legacy_v0)
    with closing(sqlite3.connect(legacy_v0)) as conn:
        conn.execute("PRAGMA user_version = 0")
    try:
        v0_counts = backup.verify_database(
            legacy_v0,
            backup.DATABASES["usage_data.db"],
            source_name="usage_data.db",
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
    staged_attendance = stage / "usage_data.db"
    with closing(sqlite3.connect(staged_attendance)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        forbidden = conn.execute(
            "SELECT forbidden_count FROM users WHERE guild_id = 7 AND user_id = 8"
        ).fetchone()
    check(
        "staged historical attendance migrates before installation",
        version == 4
        and "ai_usage" in tables
        and "point_ledger" not in tables
        and forbidden == (3,),
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
        settings_version == 6
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
            backup.DATABASES["usage_data.db"],
            source_name="usage_data.db",
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
        SQLiteUsageRepository(
            backup.DATA_DIR / "usage_data.db"
        ).increment_forbidden_count(_TEST_GUILD, 1)
        SQLitePartyRepository(
            backup.DATA_DIR / "party_data.db"
        ).create_party(_TEST_GUILD, "LOL", 2_000_000_000)
        SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
        SQLiteGameUidRepository(backup.DATA_DIR / "game_uid_data.db")
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


def test_backup_same_timestamp_rejected():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR / "collision_data"
    backup.BACKUP_DIR = _TMP_DIR / "collision_backups"
    backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
    usage_repository = SQLiteUsageRepository(
        backup.DATA_DIR / "usage_data.db"
    )
    usage_repository.increment_forbidden_count(_TEST_GUILD, 1)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
    SQLiteGameUidRepository(backup.DATA_DIR / "game_uid_data.db")
    fixed = datetime.datetime(2026, 7, 28, 12, tzinfo=datetime.timezone.utc)
    manifest = backup.create_backup_set(fixed)

    usage_repository.increment_forbidden_count(_TEST_GUILD, 2)
    try:
        backup.create_backup_set(fixed)
        check("동일 시각 백업 충돌 거부", False)
    except RuntimeError:
        check("동일 시각 백업 충돌 거부", True)
    result = backup.verify_backup_set(manifest)
    check("충돌 후 기존 백업 보존", result["usage_data.db"]["users"] == 1)


def test_prune_requires_timestamp_bound_filenames():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR / "retention_data"
    backup.BACKUP_DIR = _TMP_DIR / "retention_backups"
    backup.SETTINGS_DIR = _TMP_DIR / "backup-settings"
    backup.BACKUP_RETENTION_DAYS = 30
    SQLiteUsageRepository(
        backup.DATA_DIR / "usage_data.db"
    ).increment_forbidden_count(_TEST_GUILD, 1)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
    SQLiteGameUidRepository(backup.DATA_DIR / "game_uid_data.db")
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
    SQLiteUsageRepository(
        backup.DATA_DIR / "usage_data.db"
    ).increment_forbidden_count(_TEST_GUILD, 1)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        _TEST_GUILD, "LOL",
        2_000_000_000,
    )
    SQLiteGuildSettingsRepository(backup.DATA_DIR / "guild_settings.db")
    SQLiteGameUidRepository(backup.DATA_DIR / "game_uid_data.db")
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
            source_name="usage_data.db",
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


def _sha256_file(path: pathlib.Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_export():
    """삭제 예정 데이터 export가 원본을 건드리지 않고 전부 담아내는지."""
    print("\n[N] 삭제 예정 데이터 export")
    from module import export_legacy

    root = _TMP_DIR / "legacy_export_src"
    root.mkdir(parents=True, exist_ok=True)
    legacy_attendance_database_path = root / "usage_data.db"
    settings_db = root / "guild_settings.db"
    _create_legacy_attendance_db(legacy_attendance_database_path, version=2)
    with closing(sqlite3.connect(legacy_attendance_database_path)) as conn:
        conn.execute("INSERT INTO users VALUES (7, 99, 1234, '2026-08-17', 3)")
        conn.commit()
    with closing(sqlite3.connect(settings_db)) as conn:
        conn.executescript("""
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                music_channel_id INTEGER,
                music_panel_msg_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO guild_settings VALUES (7, 100, 200, 300, 1);
            PRAGMA user_version = 2;
        """)

    before = (
        _sha256_file(legacy_attendance_database_path),
        _sha256_file(settings_db),
    )
    document = export_legacy.build_export(root)
    sections = document["sections"]

    check("스키마 버전 기록", document["schema_versions"]["usage_data.db"] == 2)
    check("users 2행 추출", len(sections["users"]) == 2, f"({sections['users']})")
    check(
        "포인트와 최종 출석일 보존",
        {"guild_id": 7, "user_id": 99, "points": 1234,
         "last_attendance_date": "2026-08-17"} in sections["users"],
        f"({sections['users']})",
    )
    check("원장 추출", len(sections["point_ledger"]) == 1)
    check("원장 사유 보존", sections["point_ledger"][0]["reason"] == "attendance")
    check(
        "음악 채널 설정 추출",
        sections["music_settings"] == [
            {"guild_id": 7, "music_channel_id": 200, "music_panel_msg_id": 300}
        ],
        f"({sections['music_settings']})",
    )
    check(
        "export는 원본 DB를 수정하지 않음",
        before
        == (
            _sha256_file(legacy_attendance_database_path),
            _sha256_file(settings_db),
        ),
    )

    # 이미 마이그레이션이 끝난 DB — 사라진 것은 null, 남은 것은 그대로.
    migrated = _TMP_DIR / "legacy_export_migrated"
    migrated.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(migrated / "usage_data.db")) as conn:
        conn.executescript("""
            CREATE TABLE users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                forbidden_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            PRAGMA user_version = 3;
        """)
    migrated_sections = export_legacy.build_export(migrated)["sections"]
    check("마이그레이션 후 users는 null", migrated_sections["users"] is None)
    check("마이그레이션 후 원장은 null", migrated_sections["point_ledger"] is None)
    check("DB 파일이 없으면 null", migrated_sections["music_settings"] is None)

    # 빈 디렉터리에서도 죽지 않는다.
    empty = _TMP_DIR / "legacy_export_empty"
    empty.mkdir(parents=True, exist_ok=True)
    check(
        "DB가 하나도 없어도 동작",
        all(
            rows is None
            for rows in export_legacy.build_export(empty)["sections"].values()
        ),
    )

    destination = _TMP_DIR / "legacy_export_out" / "export.json"
    export_legacy.write_export(destination, root)
    written = json.loads(destination.read_text(encoding="utf-8"))
    check("파일로 기록", written["sections"]["point_ledger"][0]["delta"] == 9000)
    try:
        export_legacy.write_export(destination, root)
        clobbered = True
    except FileExistsError:
        clobbered = False
    check("기존 export 파일을 덮어쓰지 않음", not clobbered)


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
        test_requirements_lock_contract()
        test_deployment_contracts()
        test_deployment_contracts_skip_compose_when_cli_missing()
        test_forbidden_words_degrade_gracefully()
        test_forbidden_words_load_logs_to_stdout()
        test_startup_syncs_commands_globally()
        test_startup_preverification_failure_stops_cogs_and_sync()
        test_startup_migrates_legacy_attendance_before_strict_verification()
        test_startup_requires_cross_cog_dependencies()
        test_startup_cog_failure_stops_postverification_and_sync()
        test_instance_lock_rejects_second_holder()
        test_instance_lock_closes_failed_handle()
        test_main_holds_instance_lock_while_bot_runs()
        test_importing_main_does_not_construct_bot()
        test_bot_disables_all_mentions()
        test_sqlite_busy_timeout()
        test_sqlite_connect_tolerates_vanishing_wal_sidecar()
        test_backup_reads_wal_without_writer()
        test_guild_isolation()
        test_temp_image_lifecycle()
        repo = test_schema_initialization()
        test_schema_versions()
        test_ai_usage_atomicity()
        test_party_repository()
        test_party_capacity_constraint()
        test_party_cog_uses_epoch_seconds()
        test_persistent_party_panel_contract()
        test_factory()
        test_cog_facade()
        test_channel_sessions()
        test_imports()
        test_repository_layer_is_the_only_sql_surface()
        test_backup_round_trip()
        test_settings_backup_round_trip()
        test_setting_source_inspection_failure_closes_descriptor()
        test_legacy_backup_restore_and_prune()
        test_invalid_backup_settings_prevent_creation()
        test_invalid_retention_prevents_pruning()
        test_invalid_interval_prevents_loop_entry()
        test_backup_same_timestamp_rejected()
        test_prune_requires_timestamp_bound_filenames()
        test_backup_publication_is_synced()
        test_corrupt_backup_rejected()
        test_malformed_manifest_rejected()
        test_prune_skips_invalid_utf8_manifest()
        test_legacy_export()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # 임시 DB 정리

    print(f"\n{'='*40}\n결과: {PASS} 통과 / {FAIL} 실패")
    sys.exit(1 if FAIL else 0)
