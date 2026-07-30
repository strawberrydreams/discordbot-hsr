# 콘솔 전용 테스트 (디스코드 연결 불필요)
# 실행: python -m test.console_tests (프로젝트 루트에서)
#
# 검증 항목:
#   1. SQLite 마이그레이션 (구버전 users 테이블에 luckybox 컬럼 추가)
#   2. deduct_points 원자성 (동시 차감 시 잔액이 음수가 되지 않음)
#   3. play_luckybox 단일 트랜잭션 (일일 제한 / 잔액 부족 / 정산 / 동시성)
#   4. PartyRepository CRUD (파티 생성/참가/탈퇴/만료 정리)
#   5. Repository 팩토리 (sqlite 선택, 미지원 백엔드 거부)
#   6. AttendanceCog 파사드 (Repository 주입 및 위임)
#   7. 채널별 대화 세션 분리 (히스토리 독립)
#   8. 전체 모듈 import 스모크 테스트
#
# 모든 테스트는 임시 디렉터리의 격리된 DB를 사용하므로 운영 데이터를 건드리지 않는다.

import datetime
import asyncio
import importlib
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
from contextlib import closing, redirect_stdout
from io import StringIO
from unittest.mock import patch

from dotenv import dotenv_values

# module.* import 전에 환경 변수를 설정해야 함
_TMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="hsr_test_"))
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ["DATA_DIR"] = str(_TMP_DIR)
os.environ["FORBIDDEN_WORDS_FILE"] = str(_TMP_DIR / "forbidden_words.json")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("GOOGLE_API_KEY", "test-dummy")

import module.database as database
from module.database import (
    SQLiteAttendanceRepository,
    SQLitePartyRepository,
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


def test_config_paths():
    import module.config as config

    check("PROJECT_ROOT는 절대 경로", config.PROJECT_ROOT.is_absolute())
    check("DATA_DIR는 절대 경로", config.DATA_DIR.is_absolute())
    check("BACKUP_DIR는 절대 경로", config.BACKUP_DIR.is_absolute())
    check("금지어 경로는 절대 경로", config.FORBIDDEN_WORDS_FILE.is_absolute())
    check(
        "금지어 파일은 DATA_DIR에 저장",
        config.FORBIDDEN_WORDS_FILE == config.DATA_DIR / "forbidden_words.json",
    )
    check(
        "runtime 금지어 파일은 Git ignore",
        subprocess.run(
            ["git", "check-ignore", "-q", "runtime/data/forbidden_words.json"],
            cwd=PROJECT_ROOT,
            check=False,
        ).returncode
        == 0,
    )
    check(
        "실제 금지어 파일은 Git 비추적",
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "runtime/data/forbidden_words.json"],
            cwd=PROJECT_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0,
    )
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


def test_forbidden_words_default_uses_data_dir():
    import module.config as config

    original_env = os.environ.copy()
    data_dir = (_TMP_DIR / "isolated-config-data").resolve()
    try:
        os.environ["DATA_DIR"] = str(data_dir)
        os.environ.pop("FORBIDDEN_WORDS_FILE", None)
        with patch("dotenv.load_dotenv"):
            importlib.reload(config)
        default_path = config.FORBIDDEN_WORDS_FILE
    finally:
        os.environ.clear()
        os.environ.update(original_env)
        importlib.reload(config)

    check(
        "금지어 기본 경로는 DATA_DIR 사용",
        default_path == data_dir / "forbidden_words.json",
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
            "RECRUIT_CHANNEL_ID",
            "EVENT_CHANNEL_ID",
            "DISCORD_GUILD_ID",
        )
        if hasattr(config, name)
    }
    try:
        config.DISCORD_TOKEN = "test-token"
        config.OPENAI_API_KEY = "test-openai"
        config.GOOGLE_API_KEY = "test-google"
        config.RECRUIT_CHANNEL_ID = 1
        config.EVENT_CHANNEL_ID = 1
        config.DISCORD_GUILD_ID = 0
        try:
            config.validate_config()
            check("운영 길드 ID 누락 거부", False)
        except RuntimeError as exc:
            check("운영 길드 ID 누락 거부", "DISCORD_GUILD_ID" in str(exc))

        config.DISCORD_GUILD_ID = -1
        try:
            config.validate_config()
            check("운영 길드 ID 음수 거부", False)
        except RuntimeError as exc:
            check("운영 길드 ID 음수 거부", "DISCORD_GUILD_ID" in str(exc))
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
        "RECRUIT_CHANNEL_ID=123\n"
        "OVERLAP_TEST=runtime-loses\n",
        encoding="utf-8",
    )

    names = (
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "RECRUIT_CHANNEL_ID",
        "OVERLAP_TEST",
    )
    original = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["GOOGLE_API_KEY"] = "process-wins"
        config._load_env_files(root)
        check("secrets 파일 로드", os.environ["OPENAI_API_KEY"] == "file-secret")
        check("runtime 파일 로드", os.environ["RECRUIT_CHANNEL_ID"] == "123")
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
        "RECRUIT_CHANNEL_ID",
        "EVENT_CHANNEL_ID",
        "DISCORD_GUILD_ID",
        "DATA_DIR",
        "BACKUP_DIR",
        "FORBIDDEN_WORDS_FILE",
        "BACKUP_INTERVAL_SECONDS",
        "BACKUP_RETENTION_DAYS",
        "DB_BACKEND",
    }
    check("공개 env 변수 계약", set(example) == expected)
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
    check(
        "공개 env 금지어 경로",
        example["FORBIDDEN_WORDS_FILE"] == "runtime/data/forbidden_words.json",
    )


def test_deployment_contracts():
    import plistlib

    compose_result = subprocess.run(
        [
            "docker",
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
    bot = services["bot"]
    backup = services["backup"]
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

    check("Compose 이미지는 한 번만 빌드", "build" in bot and "build" not in backup)
    check(
        "두 서비스가 같은 이미지 사용",
        bot.get("image") == backup.get("image") == "discordbot-hsr:local",
    )
    check(
        "Compose 두 서비스 secrets 파일 우선",
        all(
            [pathlib.Path(item["path"]).name for item in service["env_file"]]
            == [".env.runtime", ".env.secrets"]
            for service in (bot, backup)
        ),
    )
    check(
        "Compose runtime data 권한 분리",
        any(
            str(mount.get("source", "")).endswith("runtime/data")
            and mount.get("target") == "/app/runtime/data"
            and not mount.get("read_only", False)
            for mount in bot["volumes"]
        )
        and any(
            str(mount.get("source", "")).endswith("runtime/data")
            and mount.get("target") == "/app/runtime/data"
            and mount.get("read_only") is True
            for mount in backup["volumes"]
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
            service.get("logging", {}).get("options", {}).get("max-file") == "5"
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
        and all(len(entry) == 6 for entry in newsyslog_entries)
        and {entry[0] for entry in newsyslog_entries}
        == {
            bot_plist["StandardOutPath"],
            bot_plist["StandardErrorPath"],
            backup_plist["StandardOutPath"],
            backup_plist["StandardErrorPath"],
        },
    )


def test_forbidden_words_fail_fast():
    from module.forbiddenfilter_cog import load_forbidden_words

    missing = _TMP_DIR / "missing-forbidden.json"
    try:
        load_forbidden_words(missing)
        check("금지어 파일 누락 거부", False)
    except RuntimeError:
        check("금지어 파일 누락 거부", True)

    invalid = _TMP_DIR / "invalid-forbidden.json"
    invalid.write_text("{}", encoding="utf-8")
    try:
        load_forbidden_words(invalid)
        check("금지어 JSON 구조 오류 거부", False)
    except RuntimeError:
        check("금지어 JSON 구조 오류 거부", True)


def test_forbidden_words_load_logs_to_stdout():
    import module.forbiddenfilter_cog as forbiddenfilter_cog

    words = _TMP_DIR / "forbidden-log.json"
    words.write_text('["금지어"]', encoding="utf-8")
    output = StringIO()
    with patch.object(forbiddenfilter_cog, "DATA_FILE", words), redirect_stdout(output):
        forbiddenfilter_cog.ForbiddenFilterCog(bot=None)

    check(
        "금지어 로드 로그가 stdout에 기록",
        output.getvalue().strip() == "📥 금지어 1개 로드",
    )


def test_new_install_verifies_after_loading_cogs():
    import module.main as main

    events = []

    class FakeTree:
        def copy_global_to(self, *, guild):
            events.append(f"copy:{guild.id}")

        async def sync(self, *, guild):
            events.append(f"sync:{guild.id}")

    class FakeBot:
        tree = FakeTree()

        async def load_extension(self, extension):
            events.append(f"load:{extension}")

    def verify(path, tables):
        events.append(f"verify:{path.name}")
        return {}

    with patch.object(pathlib.Path, "exists", return_value=False), \
         patch.object(main, "verify_database", side_effect=verify), \
         patch.object(main, "DISCORD_GUILD_ID", 123):
        asyncio.run(main.MyBot.setup_hook(FakeBot()))

    expected = [
        *(f"load:{extension}" for extension in main.EXTENSIONS),
        "verify:attendance_data.db",
        "verify:party_data.db",
        "copy:123",
        "sync:123",
    ]
    check("신규 설치는 Cog 로드 전 DB 검증 생략", events == expected, f"({events})")


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

    def fail_verify(path, tables):
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


def test_startup_cog_failure_stops_postverification_and_sync():
    import module.main as main

    events = []

    class FakeBot:
        class tree:
            @staticmethod
            async def sync():
                events.append("sync")

        async def load_extension(self, extension):
            events.append(f"load:{extension}")
            if extension == main.EXTENSIONS[1]:
                raise RuntimeError("broken cog")

    def verify(path, tables):
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
            "verify:attendance_data.db",
            "verify:party_data.db",
            f"load:{main.EXTENSIONS[0]}",
            f"load:{main.EXTENSIONS[1]}",
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
         patch.object(pathlib.Path, "mkdir"), \
         patch.object(main, "acquire_instance_lock", side_effect=acquire), \
         patch.object(main, "MyBot", FakeBot):
        main.main()

    check(
        "봇 실행 수명 동안 인스턴스 잠금 유지",
        events == ["acquire:.bot.lock", "lock", "run", "unlock"],
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
    import module.main as main
    from discord.ext import commands

    with patch.object(commands.Bot, "__init__", return_value=None) as init:
        main.MyBot()

    allowed = init.call_args.kwargs["allowed_mentions"]
    check("봇 전역 멘션 차단", allowed.everyone is False and allowed.roles is False and allowed.users is False)


def test_migration() -> SQLiteAttendanceRepository:
    print("\n[1] SQLite 마이그레이션")
    db_path = _TMP_DIR / "attendance_migration.db"

    # 구버전 스키마 (luckybox 컬럼 없음)를 미리 생성
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("""
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0,
                last_attendance_date TEXT,
                forbidden_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO users (user_id, points) VALUES (1, 500)")
        conn.commit()

    repo = SQLiteAttendanceRepository(db_path)  # __init__에서 마이그레이션 수행

    with closing(sqlite3.connect(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    check("luckybox_count 컬럼 추가됨", "luckybox_count" in cols)
    check("last_luckybox_date 컬럼 추가됨", "last_luckybox_date" in cols)
    check("기존 데이터 유지됨", repo.get_points(1) == 500)

    # 중복 실행해도 에러가 없어야 함 (멱등성)
    SQLiteAttendanceRepository(db_path)
    check("마이그레이션 재실행 시 에러 없음 (멱등성)", True)
    return repo


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


def test_play_luckybox(repo: SQLiteAttendanceRepository):
    print("\n[3] play_luckybox 단일 트랜잭션")
    user = 200
    today = "2026-06-11"
    repo.add_points(user, 10_000)

    # 잔액 부족
    status, result = repo.play_luckybox(user, 99_999, today, 1.0)
    check("잔액 부족 시 'insufficient' 반환", status == "insufficient")
    check("잔액 부족 시 보유 포인트 보고", result["points"] == 10_000)
    check("잔액 부족 시 잔액 변동 없음", repo.get_points(user) == 10_000)

    # 정상 베팅 (배율 고정으로 정산 검증): 1,000 베팅 x 2.5 = 2,500 획득
    status, result = repo.play_luckybox(user, 1_000, today, 2.5)
    check("정상 베팅 'ok' 반환", status == "ok")
    check("획득량 계산 일치 (2,500)", result["result_amount"] == 2_500)
    check("최종 잔액 일치 (10,000 - 1,000 + 2,500)", result["final_points"] == 11_500)
    check("DB 잔액과 반환값 일치", repo.get_points(user) == 11_500)

    # 일일 제한: 2회 더 플레이하면 3회 도달, 4번째는 거부
    repo.play_luckybox(user, 100, today, 1.0)
    repo.play_luckybox(user, 100, today, 1.0)
    status, _ = repo.play_luckybox(user, 100, today, 1.0)
    check("하루 3회 초과 시 'limit' 반환", status == "limit")

    # 날짜가 바뀌면 카운트 초기화
    status, _ = repo.play_luckybox(user, 100, "2026-06-12", 1.0)
    check("날짜 변경 시 카운트 초기화", status == "ok")

    # 동시성: 새 유저, 30개 스레드가 동시에 베팅 -> 성공은 정확히 3회(일일 제한)
    user2 = 201
    repo.add_points(user2, 1_000_000)
    statuses = []
    lock = threading.Lock()

    def worker():
        s, _ = repo.play_luckybox(user2, 1_000, today, 1.0)  # 배율 1.0 = 잔액 불변
        with lock:
            statuses.append(s)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads: t.start()
    for t in threads: t.join()

    ok_count = statuses.count("ok")
    check("동시 베팅: 일일 제한(3회)만 성공", ok_count == 3, f"(성공 {ok_count}회)")
    check("동시 베팅: 잔액 정확 (배율 1.0이므로 불변)", repo.get_points(user2) == 1_000_000,
          f"(잔액 {repo.get_points(user2)})")


def test_party_repository():
    print("\n[4] PartyRepository CRUD")
    repo = SQLitePartyRepository(_TMP_DIR / "party_test.db")
    now = 2_000_000_000

    check("없는 파티 조회 시 None", repo.get_party("LOL") is None)
    check("없는 파티 참가 거부", repo.add_participant("missing", 99) is False)
    check("거부된 참가자는 고아 행을 남기지 않음", repo.get_user_party(99) is None)

    repo.create_party("LOL", now)
    check("파티 시각을 epoch 정수로 저장", repo.get_party("LOL") == (now,))

    repo.create_party("LOL", now)  # INSERT OR IGNORE
    check("중복 생성은 무시됨", True)

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
    check("만료 파티만 삭제됨", expired == ["PUBG"], f"(삭제 목록 {expired})")
    check("만료 파티 참가자도 정리됨", repo.get_user_party(3) is None)
    check("유효 파티는 유지됨", repo.get_party("Overwatch") is not None)

    legacy_db = _TMP_DIR / "party_legacy_time.db"
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute("CREATE TABLE parties (game TEXT PRIMARY KEY, created_at TIMESTAMP)")
        conn.execute(
            "CREATE TABLE participants (game TEXT, user_id INTEGER, role TEXT, "
            "PRIMARY KEY (game, user_id))"
        )
        conn.executemany(
            "INSERT INTO parties VALUES (?, ?)",
            (
                ("Legacy", "2026-07-29 12:00:00"),
                ("BlobLegacy", sqlite3.Binary(b"2026-07-29 12:00:00")),
                ("RealLegacy", 1_785_294_000.75),
            ),
        )
        conn.commit()

    repo_with_legacy = SQLitePartyRepository(legacy_db)
    legacy_value = repo_with_legacy.get_party("Legacy")[0]
    check(
        "legacy KST 파티 시각을 UTC epoch로 변환",
        legacy_value == 1_785_294_000 and isinstance(legacy_value, int),
        f"(변환값 {legacy_value!r})",
    )
    SQLitePartyRepository(legacy_db)
    check(
        "legacy 파티 시각 마이그레이션은 재실행해도 동일",
        repo_with_legacy.get_party("Legacy") == (1_785_294_000,),
    )
    check(
        "BLOB legacy 파티 시각도 UTC epoch로 변환",
        repo_with_legacy.get_party("BlobLegacy") == (1_785_294_000,),
    )
    real_value = repo_with_legacy.get_party("RealLegacy")
    check(
        "REAL legacy epoch도 정수로 정규화",
        real_value == (1_785_294_000,)
        and isinstance(real_value[0], int),
        f"(변환값 {real_value!r})",
    )

    null_db = _TMP_DIR / "party_legacy_null_time.db"
    with closing(sqlite3.connect(null_db)) as conn:
        conn.execute("CREATE TABLE parties (game TEXT PRIMARY KEY, created_at TIMESTAMP)")
        conn.execute("INSERT INTO parties VALUES (?, ?)", ("NullLegacy", None))
        conn.commit()
    try:
        null_value = SQLitePartyRepository(null_db).get_party("NullLegacy")
    except ValueError:
        null_value = None
    check(
        "NULL legacy 파티 시각을 epoch 0으로 정규화",
        null_value == (0,) and isinstance(null_value[0], int),
        f"(변환값 {null_value!r})",
    )


def test_party_cog_uses_epoch_seconds():
    from module.playwith_cog import PlayWithCog

    class RecordingRepository:
        created_at = None
        cutoff = None

        def create_party(self, game, created_at):
            self.created_at = created_at

        def delete_expired_parties(self, cutoff):
            self.cutoff = cutoff
            return []

    repository = RecordingRepository()
    cog = object.__new__(PlayWithCog)
    cog.db = repository
    with patch("time.time", return_value=2_000_000_000.75):
        cog.create_party("LOL")
        asyncio.run(PlayWithCog.cleanup_parties.coro(cog))

    check("Cog 파티 생성 시 epoch 정수 전달", repository.created_at == 2_000_000_000)
    check(
        "Cog 만료 정리 시 24시간 전 epoch 정수 전달",
        repository.cutoff == 1_999_913_600,
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
    repo = SQLiteAttendanceRepository(_TMP_DIR / "facade_test.db")
    cog = AttendanceCog(bot=None, repository=repo)

    cog.add_points(42, 1_500)
    check("add_points 위임", repo.get_points(42) == 1_500)
    check("get_points 위임", cog.get_points(42) == 1_500)
    check("deduct_points 위임", cog.deduct_points(42, 500) is True and repo.get_points(42) == 1_000)

    cog.increment_forbidden_count(42)
    cog.increment_forbidden_count(42)
    check("forbidden_count 위임", cog.get_forbidden_count(42) == 2)


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
        "module.attendance_cog",
        "module.playwith_cog",
        "module.eventnotice_cog",
        "module.forbiddenfilter_cog",
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


def test_backup_round_trip():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR
    backup.BACKUP_DIR = _TMP_DIR / "backups"
    SQLiteAttendanceRepository(_TMP_DIR / "attendance_data.db").add_points(77, 1234)
    SQLitePartyRepository(_TMP_DIR / "party_data.db").create_party(
        "LOL",
        2_000_000_000,
    )

    manifest = backup.create_backup_set()
    result = backup.verify_backup_set(manifest)
    check("출석 DB 백업 검증", result["attendance_data.db"]["users"] == 1)
    check("파티 DB 백업 검증", result["party_data.db"]["parties"] == 1)
    backup.restore_test(manifest)
    check("백업 복구 테스트", True)


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
        backup.BACKUP_INTERVAL_SECONDS = 21600
        backup.BACKUP_RETENTION_DAYS = 30
        SQLiteAttendanceRepository(
            backup.DATA_DIR / "attendance_data.db"
        ).add_points(1, 100)
        SQLitePartyRepository(
            backup.DATA_DIR / "party_data.db"
        ).create_party("LOL", 2_000_000_000)
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
    attendance = SQLiteAttendanceRepository(
        backup.DATA_DIR / "attendance_data.db"
    )
    attendance.add_points(1, 100)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        "LOL",
        2_000_000_000,
    )
    fixed = datetime.datetime(2026, 7, 28, 12, tzinfo=datetime.timezone.utc)
    manifest = backup.create_backup_set(fixed)

    attendance.add_points(2, 200)
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
    backup.BACKUP_RETENTION_DAYS = 30
    SQLiteAttendanceRepository(
        backup.DATA_DIR / "attendance_data.db"
    ).add_points(1, 100)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        "LOL",
        2_000_000_000,
    )
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
    SQLiteAttendanceRepository(
        backup.DATA_DIR / "attendance_data.db"
    ).add_points(1, 100)
    SQLitePartyRepository(backup.DATA_DIR / "party_data.db").create_party(
        "LOL",
        2_000_000_000,
    )
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

    publication_events = events[-8:]
    check(
        "DB 파일은 공개 전에 동기화",
        publication_events[:4]
        == ["fsync:file", "fsync:file", "replace:db", "replace:db"],
    )
    check(
        "DB rename은 manifest 공개 전에 디렉터리 동기화",
        publication_events[4:6] == ["fsync:dir", "fsync:file"],
    )
    check(
        "manifest rename 후 디렉터리 동기화",
        publication_events[6:] == ["replace:manifest", "fsync:dir"],
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
        backup.verify_database(corrupt, {"users"})
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
        test_forbidden_words_default_uses_data_dir()
        test_config_validation()
        test_split_env_loading()
        test_public_env_contract()
        test_deployment_contracts()
        test_forbidden_words_fail_fast()
        test_forbidden_words_load_logs_to_stdout()
        test_new_install_verifies_after_loading_cogs()
        test_startup_preverification_failure_stops_cogs_and_sync()
        test_startup_cog_failure_stops_postverification_and_sync()
        test_instance_lock_rejects_second_holder()
        test_instance_lock_closes_failed_handle()
        test_main_holds_instance_lock_while_bot_runs()
        test_importing_main_does_not_construct_bot()
        test_bot_disables_all_mentions()
        repo = test_migration()
        test_deduct_points_atomicity(repo)
        test_attendance_atomicity(repo)
        test_play_luckybox(repo)
        test_party_repository()
        test_party_cog_uses_epoch_seconds()
        test_factory()
        test_cog_facade()
        test_channel_sessions()
        test_imports()
        test_backup_round_trip()
        test_invalid_backup_settings_prevent_creation()
        test_invalid_retention_prevents_pruning()
        test_invalid_interval_prevents_loop_entry()
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
