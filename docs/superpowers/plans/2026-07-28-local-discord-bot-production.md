# Local Discord Bot Production Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 공개 GitHub 저장소 한 개를 유지하면서 macOS `launchd` 또는 Docker Desktop에서 Discord 봇을 안정적으로 실행하고, SQLite 온라인 백업·자동 복구 검증·실패 시 시작 중단·일괄 재시작 배포를 제공한다.

**Architecture:** 소스는 Git이 추적하고 실제 비밀값·금지어·DB·백업은 같은 프로젝트 폴더의 ignore된 런타임 파일로 둔다. SQLite는 유지하며 Python 표준 `sqlite3.Connection.backup()`으로 실행 중 백업을 만들고, 백업마다 해시·구조·복구 가능성을 검증한다. 코드 변경은 Cog hot reload 없이 테스트와 백업을 통과한 뒤 `launchd` 또는 Docker Compose로 프로세스를 한 번 재시작한다.

**Tech Stack:** Python 3.12+, discord.py 2.7.1, python-dotenv 1.2.2, SQLite, macOS launchd, Docker Desktop, Docker Compose

## Global Constraints

- 프로젝트 작업 폴더는 `/Users/strawberrydreams/coding/discordbot-hsr` 하나만 사용한다.
- 공개 GitHub 저장소에는 실제 API 키, 금지어 목록, SQLite DB, 백업, 로그를 올리지 않는다.
- SQLite를 유지하고 외부 DB 구현은 추가하지 않는다.
- 백업 검증은 자동화하지만 운영 DB에 대한 실제 복원은 봇을 정지한 뒤 명시적으로 수행한다.
- Cog hot reload, 다중 봇 blue-green 배포, 자동 운영 DB 덮어쓰기는 구현하지 않는다.
- 운영 변경은 테스트 → 온라인 백업 → 백업 검증 → 한 번의 재시작 순서로 적용한다.
- 새 런타임 의존성은 추가하지 않고 Python 표준 라이브러리와 이미 설치된 `python-dotenv`를 사용한다.
- Docker 컨테이너는 소스를 이미지에 포함하고 DB·백업·금지어만 bind mount한다.

---

## Current Baseline

- Git clone은 정상이며 shallow clone이 아니다.
- `main`과 `origin/main`은 `bf20cb0369d7785a429aee9a7e076d4f2c06e904`로 일치한다.
- 전체 이력은 47개 commit이며 `git fsck --full --no-dangling`이 성공한다.
- 현재 작업 트리는 clean이다.
- `settings/forbidden_words.json`과 `settings/NULL_API_KEY.env`는 현재 Git에 추적되고 있다.
- `settings/forbidden_words.json`은 `78debfe` commit부터 Git 이력에 존재한다.
- 전체 객체 휴리스틱 검사에서 과거 `single/active/hyacine_gpt_slash.py`에 OpenAI 키 형태 문자열이 발견됐다.
- 실행 가능한 `.venv`는 없으므로 구현 전 새 가상환경이 필요하다.
- 현재 `.gitignore`에는 `.DS_Store`만 있다.
- 현재 `module/config.py`는 실행 디렉터리에 상대적인 `settings/` 경로와 세 개의 개별 env 파일을 가정한다.
- 현재 `module/main.py`는 Cog 로딩 오류를 출력한 뒤 계속 시작한다.
- 현재 금지어 로더는 파일 누락·파싱 실패 시 빈 목록을 반환해 필터를 조용히 비활성화한다.

## Manual Security Gate

이 절은 자동 구현 작업에 포함하지 않는다. 저장소 소유자가 결과와 영향을 확인한 뒤 별도로 승인한다.

1. 과거 OpenAI 키 형태 문자열이 실제 키였는지 확인한다.
2. 실제 키였다면 OpenAI 콘솔에서 폐기하고 새 키를 발급한다.
3. 공개 이력에서 금지어 JSON을 제거할지 결정한다.
4. 제거를 선택하면 보호 브랜치·열린 PR·fork 영향을 확인한 뒤 `git filter-repo`로 `settings/forbidden_words.json` 경로를 모든 refs에서 제거한다.
5. 이력 재작성 후에는 모든 commit hash가 바뀌므로 GitHub에 force push하기 전에 별도 승인을 받는다.

이 계획의 코드 구현은 키 폐기 후 진행할 수 있다. 금지어 이력 재작성은 코드 구현 전후 어느 시점에도 가능하지만, 공개 저장소에서 과거 목록까지 숨기는 요구를 충족하려면 최종 배포 전에 완료해야 한다.

## Implementation Prerequisite

현재 clone에는 실행 가능한 `.venv`가 없으므로 Task 1을 시작하기 전에 기준 환경을 만든다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m test.console_tests
```

Expected: 기존 기준 테스트가 exit status 0으로 끝난다. 실패하면 구현을 시작하지 않고 기존 코드 또는 설치 환경 문제를 먼저 기록한다.

## File Responsibility Map

### Create

- `.dockerignore`: Docker build context에서 비밀값과 런타임 데이터를 제외한다.
- `.env.example`: 공개 가능한 환경변수 이름과 안전한 기본값을 제공한다.
- `settings/forbidden_words.example.json`: 공개 가능한 금지어 파일 형식을 제공한다.
- `module/backup.py`: SQLite 백업 생성, 검증, 복구 테스트, 보존 기간 정리, 반복 실행 CLI를 담당한다.
- `Dockerfile`: 재현 가능한 Python 런타임 이미지를 만든다.
- `compose.yaml`: 봇과 백업 프로세스 및 bind mount를 정의한다.
- `deploy/macos/com.discordbot.hsr.plist.example`: 봇 LaunchAgent 템플릿이다.
- `deploy/macos/com.discordbot.hsr-backup.plist.example`: 주기적 백업 LaunchAgent 템플릿이다.

### Modify

- `.gitignore`: 실제 설정, 금지어, DB, 백업, 로그, 가상환경을 제외한다.
- `module/config.py`: 프로젝트 루트 기반 절대 경로, 단일 `.env`, 타입 변환, 운영 설정 검증을 제공한다.
- `module/main.py`: 설정 검증, Cog fail-fast, DB 검증 후 명령 동기화를 수행한다.
- `module/forbiddenfilter_cog.py`: 금지어 파일 오류를 시작 실패로 승격한다.
- `test/console_tests.py`: 설정, 백업, 손상 탐지, 복구 테스트, 금지어 fail-fast 검증을 추가한다.
- `test/hello_prefix.py`: 단일 루트 `.env`를 사용하도록 맞춘다.
- `README.md`: 공개 저장소, 설정, launchd, Docker, 백업, 복원, 배포, 롤백 절차를 설명한다.

### Keep

- `module/database.py`: Repository와 SQLite 트랜잭션 구현을 유지한다.
- `requirements.txt`: 새 패키지를 추가하지 않는다.
- 기존 Cog 파일: 기능 로직은 변경하지 않는다.

---

### Task 1: Repository Privacy Boundary

**Files:**
- Modify: `.gitignore`
- Create: `.dockerignore`
- Create: `.env.example`
- Create: `settings/forbidden_words.example.json`
- Remove from tracking: `settings/NULL_API_KEY.env`
- Remove from tracking but keep locally: `settings/forbidden_words.json`

**Interfaces:**
- Consumes: 현재 clean Git 작업 트리
- Produces: Git과 Docker가 공통으로 적용하는 공개/비공개 파일 경계

- [ ] **Step 1: Write the expected ignore contract**

`.gitignore`의 기대 내용은 다음과 같다.

```gitignore
.DS_Store

# Secrets and private moderation data
.env
settings/*.env
settings/forbidden_words.json

# Runtime state
runtime/
data/
backups/
logs/
temp_images/
*.db

# Python
__pycache__/
*.pyc
.venv/
venv/
```

- [ ] **Step 2: Verify the current repository violates the contract**

Run:

```bash
git ls-files settings/forbidden_words.json settings/NULL_API_KEY.env
```

Expected: 두 파일 경로가 모두 출력된다.

- [ ] **Step 3: Add `.gitignore` and `.dockerignore`**

`.dockerignore` 내용:

```dockerignore
.git
.gitignore
.DS_Store
.venv
venv
.env
settings/*.env
settings/forbidden_words.json
runtime
data
backups
logs
temp_images
__pycache__
*.pyc
*.db
docs
test
single
```

- [ ] **Step 4: Add public examples**

`.env.example` 내용:

```dotenv
DISCORD_TOKEN=
OPENAI_API_KEY=
GOOGLE_API_KEY=
RECRUIT_CHANNEL_ID=
EVENT_CHANNEL_ID=
DATA_DIR=runtime/data
BACKUP_DIR=runtime/backups
FORBIDDEN_WORDS_FILE=settings/forbidden_words.json
BACKUP_INTERVAL_SECONDS=21600
BACKUP_RETENTION_DAYS=30
DB_BACKEND=sqlite
```

`settings/forbidden_words.example.json` 내용:

```json
[
  "example"
]
```

- [ ] **Step 5: Stop tracking private runtime files**

Run:

```bash
git rm settings/NULL_API_KEY.env
git rm --cached settings/forbidden_words.json
```

Expected: 더미 env 파일은 삭제되고 실제 금지어 파일은 로컬에 남지만 Git index에서는 제거된다.

- [ ] **Step 6: Verify both privacy boundaries**

Run:

```bash
git check-ignore -v .env settings/forbidden_words.json runtime/data/attendance_data.db
git ls-files settings/forbidden_words.json settings/NULL_API_KEY.env
```

Expected: 첫 명령은 각 ignore 규칙을 출력하고, 두 번째 명령은 아무것도 출력하지 않는다.

- [ ] **Step 7: Commit**

```bash
git add .gitignore .dockerignore .env.example settings/forbidden_words.example.json
git commit -m "chore: separate private runtime files"
```

---

### Task 2: Absolute Configuration and Validation

**Files:**
- Modify: `module/config.py:1-61`
- Modify: `test/console_tests.py`
- Modify: `test/hello_prefix.py:1-24`

**Interfaces:**
- Consumes: root `.env` contract from Task 1
- Produces:
  - `PROJECT_ROOT: pathlib.Path`
  - `DATA_DIR: pathlib.Path`
  - `BACKUP_DIR: pathlib.Path`
  - `FORBIDDEN_WORDS_FILE: pathlib.Path`
  - `BACKUP_INTERVAL_SECONDS: int`
  - `BACKUP_RETENTION_DAYS: int`
  - `validate_config() -> None`

- [ ] **Step 1: Add failing configuration checks**

Add checks to `test/console_tests.py`:

```python
def test_config_paths():
    import module.config as config

    check("PROJECT_ROOT는 절대 경로", config.PROJECT_ROOT.is_absolute())
    check("DATA_DIR는 절대 경로", config.DATA_DIR.is_absolute())
    check("BACKUP_DIR는 절대 경로", config.BACKUP_DIR.is_absolute())
    check("금지어 경로는 절대 경로", config.FORBIDDEN_WORDS_FILE.is_absolute())


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
```

Call both functions from the existing `__main__` test sequence.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m test.console_tests
```

Expected: `BACKUP_DIR`, `PROJECT_ROOT`, or `validate_config`가 없어 실패한다.

- [ ] **Step 3: Replace relative configuration with root-based configuration**

Use these helpers in `module/config.py`:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _path_from_env(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _int_from_env(name: str, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        if default is None:
            raise RuntimeError(f"{name} 환경변수가 필요합니다.")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 환경변수는 정수여야 합니다.") from exc
```

Define runtime values:

```python
DATA_DIR = _path_from_env("DATA_DIR", "runtime/data")
BACKUP_DIR = _path_from_env("BACKUP_DIR", "runtime/backups")
FORBIDDEN_WORDS_FILE = _path_from_env(
    "FORBIDDEN_WORDS_FILE",
    "settings/forbidden_words.json",
)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
RECRUIT_CHANNEL_ID = _int_from_env("RECRUIT_CHANNEL_ID", 0)
EVENT_CHANNEL_ID = _int_from_env("EVENT_CHANNEL_ID", 0)
BACKUP_INTERVAL_SECONDS = _int_from_env("BACKUP_INTERVAL_SECONDS", 21600)
BACKUP_RETENTION_DAYS = _int_from_env("BACKUP_RETENTION_DAYS", 30)
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").lower()
```

Remove the unused `DB_URL` and duplicate `load_forbidden_words()` from `module/config.py`.

- [ ] **Step 4: Add explicit production validation**

```python
def validate_config() -> None:
    missing = [
        name
        for name, value in (
            ("DISCORD_TOKEN", DISCORD_TOKEN),
            ("OPENAI_API_KEY", OPENAI_API_KEY),
            ("GOOGLE_API_KEY", GOOGLE_API_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"필수 환경변수가 없습니다: {', '.join(missing)}")
    if RECRUIT_CHANNEL_ID <= 0:
        raise RuntimeError("RECRUIT_CHANNEL_ID는 양의 정수여야 합니다.")
    if EVENT_CHANNEL_ID <= 0:
        raise RuntimeError("EVENT_CHANNEL_ID는 양의 정수여야 합니다.")
    if BACKUP_INTERVAL_SECONDS <= 0:
        raise RuntimeError("BACKUP_INTERVAL_SECONDS는 양의 정수여야 합니다.")
    if BACKUP_RETENTION_DAYS <= 0:
        raise RuntimeError("BACKUP_RETENTION_DAYS는 양의 정수여야 합니다.")
    if DB_BACKEND != "sqlite":
        raise RuntimeError("현재 지원하는 DB_BACKEND는 sqlite뿐입니다.")
```

- [ ] **Step 5: Update the legacy prefix test**

Replace the three-file dotenv loading in `test/hello_prefix.py` with:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")
```

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m test.console_tests
```

Expected: 모든 기존 테스트와 새 설정 테스트가 통과한다.

- [ ] **Step 7: Commit**

```bash
git add module/config.py test/console_tests.py test/hello_prefix.py
git commit -m "feat: validate absolute runtime configuration"
```

---

### Task 3: SQLite Online Backup and Restore Verification

**Files:**
- Create: `module/backup.py`
- Modify: `test/console_tests.py`

**Interfaces:**
- Consumes:
  - `DATA_DIR: pathlib.Path`
  - `BACKUP_DIR: pathlib.Path`
  - `BACKUP_INTERVAL_SECONDS: int`
  - `BACKUP_RETENTION_DAYS: int`
- Produces:
  - `verify_database(path: Path, required_tables: set[str]) -> dict[str, int]`
  - `create_backup_set(now: datetime | None = None) -> Path`
  - `verify_backup_set(manifest_path: Path) -> dict[str, dict[str, int]]`
  - `restore_test(manifest_path: Path) -> None`
  - `prune_backups(now: datetime | None = None) -> int`
  - CLI commands `create`, `verify`, `restore-test`, `loop`

- [ ] **Step 1: Add failing backup tests**

Add to `test/console_tests.py`:

```python
def test_backup_round_trip():
    import module.backup as backup

    backup.DATA_DIR = _TMP_DIR
    backup.BACKUP_DIR = _TMP_DIR / "backups"
    SQLiteAttendanceRepository(_TMP_DIR / "attendance_data.db").add_points(77, 1234)
    SQLitePartyRepository(_TMP_DIR / "party_data.db").create_party(
        "LOL",
        datetime.datetime.now(),
    )

    manifest = backup.create_backup_set()
    result = backup.verify_backup_set(manifest)
    check("출석 DB 백업 검증", result["attendance_data.db"]["users"] == 1)
    check("파티 DB 백업 검증", result["party_data.db"]["parties"] == 1)
    backup.restore_test(manifest)
    check("백업 복구 테스트", True)


def test_corrupt_backup_rejected():
    import module.backup as backup

    corrupt = _TMP_DIR / "corrupt.db"
    corrupt.write_bytes(b"not-a-sqlite-database")
    try:
        backup.verify_database(corrupt, {"users"})
        check("손상 백업 거부", False)
    except RuntimeError:
        check("손상 백업 거부", True)
```

Call both functions in the existing test sequence.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m test.console_tests
```

Expected: `module.backup` import가 실패한다.

- [ ] **Step 3: Implement read-only database verification**

`module/backup.py` starts with:

```python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from module.config import (
    BACKUP_DIR,
    BACKUP_INTERVAL_SECONDS,
    BACKUP_RETENTION_DAYS,
    DATA_DIR,
)

DATABASES = {
    "attendance_data.db": {"users"},
    "party_data.db": {"parties", "participants"},
}


def verify_database(path: Path, required_tables: set[str]) -> dict[str, int]:
    if not path.is_file():
        raise RuntimeError(f"DB 파일이 없습니다: {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError(f"SQLite 무결성 검사 실패: {path}")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = required_tables - tables
            if missing:
                raise RuntimeError(
                    f"필수 테이블이 없습니다: {', '.join(sorted(missing))}"
                )
            return {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in sorted(required_tables)
            }
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"SQLite 파일을 읽을 수 없습니다: {path}") from exc
```

Table names come only from the fixed `DATABASES` constant, so the quoted f-string does not accept user input.

- [ ] **Step 4: Implement atomic online backup creation**

Use these helpers:

```python
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_one(source: Path, temporary: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn:
        with sqlite3.connect(temporary) as target_conn:
            source_conn.backup(target_conn)
```

`create_backup_set()` must:

1. Create `BACKUP_DIR`.
2. Use UTC timestamp format `%Y%m%dT%H%M%SZ`.
3. Back up both DB files to names ending in `.tmp`.
4. Run `verify_database()` on both temporary files.
5. Rename both with `os.replace()` only after both verify successfully.
6. Write one manifest JSON through a `.tmp` file and `os.replace()`.
7. Include `created_at`, source filename, backup filename, byte size, SHA-256, and table row counts.
8. Delete remaining `.tmp` files in `finally`.
9. Call `prune_backups()` only after the manifest is committed.
10. Return the final manifest path.

Final names:

```text
20260728T120000Z-attendance_data.db
20260728T120000Z-party_data.db
20260728T120000Z-manifest.json
```

- [ ] **Step 5: Implement manifest verification and restore test**

`verify_backup_set()` must load the manifest, recompute file size and SHA-256, call `verify_database()` with the fixed required table set, and return the row counts.

`restore_test()` must:

```python
def restore_test(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="hsr_restore_") as temp_dir:
        temp_root = Path(temp_dir)
        for item in manifest["databases"]:
            source = manifest_path.parent / item["backup"]
            restored = temp_root / item["source"]
            shutil.copy2(source, restored)
            verify_database(restored, DATABASES[item["source"]])
```

- [ ] **Step 6: Implement retention**

`prune_backups()` must delete only complete sets whose manifest timestamp is older than `BACKUP_RETENTION_DAYS`. It must never delete:

- live files under `DATA_DIR`
- `.tmp` files created by another currently running process
- a DB file not referenced by the expired manifest being processed

Return the number of deleted backup sets.

- [ ] **Step 7: Implement CLI**

```python
def latest_manifest() -> Path:
    manifests = sorted(BACKUP_DIR.glob("*-manifest.json"))
    if not manifests:
        raise RuntimeError("검증할 백업 manifest가 없습니다.")
    return manifests[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("create", "verify", "restore-test", "loop"),
    )
    args = parser.parse_args()

    if args.command == "create":
        print(create_backup_set())
        return 0
    if args.command == "verify":
        verify_backup_set(latest_manifest())
        return 0
    if args.command == "restore-test":
        restore_test(latest_manifest())
        return 0

    while True:
        try:
            print(create_backup_set())
        except Exception as exc:
            print(f"백업 실패: {exc}", flush=True)
            time.sleep(min(60, BACKUP_INTERVAL_SECONDS))
            continue
        time.sleep(BACKUP_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 8: Run all backup checks**

Run:

```bash
.venv/bin/python -m test.console_tests
```

Expected: 기존 테스트, 온라인 백업, 손상 탐지, 복구 테스트가 모두 통과한다.

- [ ] **Step 9: Commit**

```bash
git add module/backup.py test/console_tests.py
git commit -m "feat: add verified sqlite backups"
```

---

### Task 4: Fail-Fast Startup

**Files:**
- Modify: `module/forbiddenfilter_cog.py:78-105`
- Modify: `module/main.py:1-65`
- Modify: `test/console_tests.py`

**Interfaces:**
- Consumes:
  - `validate_config() -> None`
  - `verify_database(path, required_tables) -> dict[str, int]`
  - existing Cog extension names
- Produces:
  - `load_forbidden_words(path: Path) -> list[str]`
  - startup that reaches `tree.sync()` only after every Cog and both DB files pass

- [ ] **Step 1: Add failing forbidden-word loader tests**

Add a pure loader test:

```python
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
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m test.console_tests
```

Expected: `load_forbidden_words`가 없어 실패한다.

- [ ] **Step 3: Extract a strict word loader**

Add:

```python
def load_forbidden_words(path: pathlib.Path) -> List[str]:
    if not path.is_file():
        raise RuntimeError(f"금지어 파일이 없습니다: {path}")
    try:
        with path.open(encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"금지어 파일을 읽을 수 없습니다: {path}") from exc
    if not isinstance(data, list):
        raise RuntimeError("금지어 JSON 최상단은 배열이어야 합니다.")
    words = [_normalize_term(str(word)) for word in data if str(word).strip()]
    if not words:
        raise RuntimeError("금지어 목록이 비어 있습니다.")
    return words
```

`ForbiddenFilterCog.__init__()` must call this function and compile the returned list. Remove the catch-and-return-empty behavior.

- [ ] **Step 4: Make Cog loading fail-fast**

In `module/main.py`, define the extension tuple once:

```python
EXTENSIONS = (
    "module.eventnotice_cog",
    "module.playwith_cog",
    "module.forbiddenfilter_cog",
    "module.hyacine_chat_cog",
    "module.hyacine_image_cog",
    "module.attendance_cog",
    "module.finance_cog",
)
```

Replace the per-extension catch with:

```python
async def setup_hook(self):
    for extension in EXTENSIONS:
        await self.load_extension(extension)
        print(f"🧩 Loaded extension: {extension}")

    verify_database(DATA_DIR / "attendance_data.db", {"users"})
    verify_database(DATA_DIR / "party_data.db", {"parties", "participants"})
    await self.tree.sync()
    print("🔄 Command tree synced")
```

An exception from any extension or DB verification must propagate out of `setup_hook`.

- [ ] **Step 5: Validate configuration before connection**

Use an explicit entry point:

```python
def main() -> None:
    validate_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    MyBot().run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
```

Do not construct a module-global bot object.

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m test.console_tests
```

Expected: 모든 테스트가 통과한다.

- [ ] **Step 7: Manual startup failure smoke check**

Create a local `.env` with one required value omitted, then run:

```bash
.venv/bin/python -m module.main
```

Expected: Discord에 연결하기 전에 누락된 변수 이름을 포함한 오류로 종료한다.

- [ ] **Step 8: Commit**

```bash
git add module/main.py module/forbiddenfilter_cog.py test/console_tests.py
git commit -m "fix: fail startup on invalid production state"
```

---

### Task 5: macOS launchd Services

**Files:**
- Create: `deploy/macos/com.discordbot.hsr.plist.example`
- Create: `deploy/macos/com.discordbot.hsr-backup.plist.example`

**Interfaces:**
- Consumes:
  - `.venv/bin/python`
  - `python -m module.main`
  - `python -m module.backup create`
- Produces:
  - crash-restarting bot LaunchAgent
  - six-hour backup LaunchAgent

- [ ] **Step 1: Create runtime directories**

Run:

```bash
mkdir -p runtime/data runtime/backups runtime/logs
```

- [ ] **Step 2: Add the bot LaunchAgent template**

`deploy/macos/com.discordbot.hsr.plist.example`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.discordbot.hsr</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/strawberrydreams/coding/discordbot-hsr/.venv/bin/python</string>
    <string>-m</string>
    <string>module.main</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/strawberrydreams/coding/discordbot-hsr</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/bot.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/bot-error.log</string>
</dict>
</plist>
```

- [ ] **Step 3: Add the backup LaunchAgent template**

`deploy/macos/com.discordbot.hsr-backup.plist.example`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
<key>Label</key>
<string>com.discordbot.hsr-backup</string>
<key>ProgramArguments</key>
<array>
  <string>/Users/strawberrydreams/coding/discordbot-hsr/.venv/bin/python</string>
  <string>-m</string>
  <string>module.backup</string>
  <string>create</string>
</array>
<key>WorkingDirectory</key>
<string>/Users/strawberrydreams/coding/discordbot-hsr</string>
<key>RunAtLoad</key>
<true/>
<key>StartInterval</key>
<integer>21600</integer>
<key>StandardOutPath</key>
<string>/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/backup.log</string>
<key>StandardErrorPath</key>
<string>/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/backup-error.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Validate plist syntax**

Run:

```bash
plutil -lint deploy/macos/com.discordbot.hsr.plist.example
plutil -lint deploy/macos/com.discordbot.hsr-backup.plist.example
```

Expected: both files report `OK`.

- [ ] **Step 5: Document local installation commands**

The README must direct the operator to copy both files to `~/Library/LaunchAgents/` without the `.example` suffix, then run:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.discordbot.hsr.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.discordbot.hsr-backup.plist
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr
```

- [ ] **Step 6: Commit**

```bash
git add deploy/macos
git commit -m "feat: add macos launch agents"
```

---

### Task 6: Docker Desktop Runtime

**Files:**
- Create: `Dockerfile`
- Create: `compose.yaml`

**Interfaces:**
- Consumes:
  - Task 1 Docker privacy boundary
  - Task 2 relative runtime path defaults
  - Task 3 `module.backup loop`
- Produces:
  - `bot` Compose service
  - `backup` Compose service

- [ ] **Step 1: Add the Dockerfile**

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY module ./module

RUN useradd --create-home bot \
    && mkdir -p /app/settings /app/runtime/data /app/runtime/backups \
    && chown -R bot:bot /app

USER bot

CMD ["python", "-m", "module.main"]
```

- [ ] **Step 2: Add Compose services**

```yaml
services:
  bot:
    build: .
    env_file:
      - .env
    command: ["python", "-m", "module.main"]
    restart: unless-stopped
    stop_grace_period: 30s
    volumes:
      - ./runtime/data:/app/runtime/data
      - ./runtime/backups:/app/runtime/backups
      - ./settings/forbidden_words.json:/app/settings/forbidden_words.json:ro

  backup:
    build: .
    env_file:
      - .env
    command: ["python", "-m", "module.backup", "loop"]
    restart: unless-stopped
    volumes:
      - ./runtime/data:/app/runtime/data:ro
      - ./runtime/backups:/app/runtime/backups
```

No port mapping is required because the Discord bot opens outbound connections.

- [ ] **Step 3: Validate Compose**

Run:

```bash
docker compose config
```

Expected: resolved `bot` and `backup` services are printed without errors.

- [ ] **Step 4: Build without private files**

Run:

```bash
docker build -t discordbot-hsr:plan-check .
```

Expected: the runtime image builds successfully.

- [ ] **Step 5: Verify image contents**

Run:

```bash
docker run --rm --entrypoint sh discordbot-hsr:plan-check -c 'test ! -e /app/.env && test ! -e /app/settings/forbidden_words.json'
```

Expected: command exits with status 0, proving the image itself contains neither file.

- [ ] **Step 6: Verify persistent data recreation**

Run:

```bash
docker compose up -d
docker compose ps
docker compose stop bot
docker compose rm -f bot
docker compose up -d bot
```

Expected: `runtime/data/*.db` remains on the macOS host and the recreated bot reads the same files.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile compose.yaml
git commit -m "feat: add docker desktop deployment"
```

---

### Task 7: Deployment and Recovery Runbook

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: all commands and files from Tasks 1-6
- Produces: one authoritative operator runbook

- [ ] **Step 1: Replace the private-repository warning**

Document that the repository may remain public because `.env`, the real forbidden-word list, runtime DBs, backups, and logs are untracked and excluded from Docker images.

- [ ] **Step 2: Document first-time setup**

Include:

```bash
cp .env.example .env
cp settings/forbidden_words.example.json settings/forbidden_words.json
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m test.console_tests
```

State that `.env` and the real forbidden-word list must be edited locally and must never be force-added to Git.

- [ ] **Step 3: Document backup operations**

```bash
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
```

Document the six-hour default, 30-day retention, and the need to include `runtime/backups/` in Time Machine or an external-disk backup. State that the active SQLite files themselves should not be placed in a cloud-synced folder.

- [ ] **Step 4: Document actual restore**

The restore runbook must require:

1. Stop `com.discordbot.hsr` or the Compose `bot` service.
2. Run `restore-test` on the selected backup.
3. Copy current live DB files to a timestamped emergency directory.
4. Copy the selected verified backup DB files into `runtime/data/`.
5. Run `PRAGMA integrity_check` through `module.backup.verify_database`.
6. Start the bot.
7. Confirm wallet, ranking, party, and forbidden-count reads.

Do not provide an unattended auto-restore command.

- [ ] **Step 5: Document batched deployment**

macOS:

```bash
git pull --ff-only
.venv/bin/python -m test.console_tests
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr
tail -n 100 runtime/logs/bot.log
```

Docker:

```bash
git pull --ff-only
.venv/bin/python -m test.console_tests
docker compose run --rm backup python -m module.backup create
docker compose run --rm backup python -m module.backup verify
docker compose build bot
docker compose up -d --no-deps bot
docker compose logs --tail=100 bot
```

State that a failed test or failed backup stops the deployment before restart.

- [ ] **Step 6: Document rollback**

Record the previous commit before pull:

```bash
git rev-parse HEAD
```

If startup fails, restore the previous commit with a normal revert or checkout approved by the operator, rerun tests, and restart once. Do not use `git reset --hard` in the runbook.

- [ ] **Step 7: Remove misleading external DB instructions**

State only that SQLite is the supported production backend. Keep the existing Repository explanation as internal architecture documentation but remove MySQL/Oracle deployment steps that are not implemented.

- [ ] **Step 8: Document host constraints**

State:

- Mac sleep suspends both LaunchAgents and Docker Desktop containers.
- Docker Desktop must start at login for Docker deployment.
- LaunchAgents run in the logged-in user session.
- A network or power outage still takes the bot offline.

- [ ] **Step 9: Commit**

```bash
git add README.md
git commit -m "docs: add local production runbook"
```

---

### Task 8: Final Production Rehearsal

**Files:**
- Verify only; no source changes expected

**Interfaces:**
- Consumes: complete implementation
- Produces: evidence that the public repository and both local deployment modes satisfy the global constraints

- [ ] **Step 1: Create a clean environment**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

- [ ] **Step 2: Run the full test suite**

```bash
.venv/bin/python -m test.console_tests
```

Expected: exit status 0 and no failed checks.

- [ ] **Step 3: Verify Git privacy**

```bash
git status --short
git ls-files .env settings/forbidden_words.json 'runtime/**' '*.db'
git check-ignore -v .env settings/forbidden_words.json runtime/data/attendance_data.db
```

Expected: no private file is tracked and all are matched by explicit ignore rules.

- [ ] **Step 4: Verify online backup**

With the bot running:

```bash
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
```

Expected: all commands exit 0 without stopping the bot.

- [ ] **Step 5: Verify fail-fast**

Temporarily point `FORBIDDEN_WORDS_FILE` at a missing file and start the bot.

Expected: no Discord ready event occurs and the process exits non-zero.

- [ ] **Step 6: Verify launchd definitions**

```bash
plutil -lint deploy/macos/com.discordbot.hsr.plist.example
plutil -lint deploy/macos/com.discordbot.hsr-backup.plist.example
```

Expected: both report `OK`.

- [ ] **Step 7: Verify Docker definitions**

```bash
docker compose config
docker compose build
```

Expected: both commands exit 0.

- [ ] **Step 8: Confirm implementation scope**

Verify that the diff contains no:

- Cog reload command
- external database driver
- automatic live-database restore
- second bot replica
- new Python package

- [ ] **Step 9: Review commit series**

```bash
git log --oneline --decorate -10
git status --short
```

Expected: the planned commits are present and the worktree is clean.

---

## Checkpoints

### Checkpoint A: After Tasks 1-2

- Git and Docker privacy rules agree.
- No real secret or moderation data is tracked.
- Configuration paths are absolute and independent of current working directory.
- Existing tests pass.

### Checkpoint B: After Tasks 3-4

- Running SQLite databases can be backed up without stopping the bot.
- Corrupt backups fail verification.
- Restore tests do not modify live data.
- Missing config, broken Cog, invalid forbidden list, or corrupt DB prevents startup.

### Checkpoint C: After Tasks 5-7

- macOS launchd definitions pass `plutil`.
- Docker definitions build and preserve host-mounted SQLite data.
- README contains first-run, update, backup, restore, rollback, and host limitation procedures.

### Checkpoint D: After Task 8

- Full test and deployment rehearsal passes.
- Worktree is clean.
- Manual security gate has an explicit owner decision.
- The project is ready for real API keys in local `.env`.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| 과거 OpenAI 키가 유효했음 | 높음 | 먼저 키를 폐기하고 새 키를 로컬 `.env`에만 저장한다. |
| 금지어 JSON이 Git 이력에 남음 | 중간 | 현재 추적을 중단하고, 과거 공개도 허용하지 않으면 승인 후 이력을 재작성한다. |
| Mac 내부 디스크 손상 | 높음 | 검증 완료 백업을 Time Machine 또는 외장 디스크에도 보관한다. |
| 자동 복원이 정상 DB를 덮어씀 | 높음 | 복구 테스트만 자동화하고 실제 복원은 정지 상태에서 명시적으로 수행한다. |
| Docker 컨테이너 재생성 | 중간 | DB와 백업을 프로젝트 폴더의 bind mount에 저장한다. |
| 업데이트 실패로 봇 중단 | 중간 | 기존 프로세스를 유지한 채 테스트와 백업을 먼저 수행하고 한 번만 재시작한다. |
| macOS sleep 또는 로그아웃 | 중간 | 전원 연결 시 sleep을 방지하고 LaunchAgent/Docker Desktop 세션 제약을 문서화한다. |
| 두 배포 방식을 동시에 실행 | 높음 | launchd와 Docker 중 하나만 활성화하도록 README에 명시한다. |

## Definition of Done

- 공개 Git에는 소스, 예제 설정, 배포 정의, 문서만 존재한다.
- 실제 `.env`, 금지어, DB, 백업, 로그는 Git과 Docker image에서 제외된다.
- SQLite 데이터는 호스트 파일로 유지된다.
- 실행 중인 DB를 정기적으로 백업할 수 있다.
- 모든 백업은 구조, 행 읽기, 크기, SHA-256으로 검증된다.
- 최신 백업은 임시 디렉터리에서 자동 복구 테스트된다.
- 필수 설정 또는 필수 Cog가 잘못되면 봇이 온라인이 되지 않는다.
- launchd와 Docker Compose 정의가 각각 검증된다.
- 변경 사항은 테스트와 백업 후 한 번의 재시작으로 적용된다.
- README만으로 초기 설치, 운영, 백업, 복원, 업데이트, 롤백을 수행할 수 있다.
