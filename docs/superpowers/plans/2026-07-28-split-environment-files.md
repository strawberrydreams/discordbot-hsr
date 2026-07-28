# Split Environment Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 공개 `.env.example`은 유지하면서 실제 자격 증명과 일반 런타임 설정을 `.env.secrets`와 `.env.runtime`으로 분리한다.

**Architecture:** `module/config.py`는 프로젝트 루트의 secrets 파일을 먼저, runtime 파일을 다음에 `override=False`로 로드한다. Git과 Docker는 실제 두 파일과 기존 `.env`를 제외하고, Compose는 두 파일을 서비스 환경으로 함께 주입한다. 기존 로컬 `.env`는 값이 출력되지 않는 일회성 이전 절차로 분리한 뒤 삭제한다.

**Tech Stack:** Python 3.12+, python-dotenv 1.2.2, Docker Compose, Git

## Global Constraints

- 프로젝트 작업 폴더는 `/Users/strawberrydreams/coding/discordbot-hsr` 하나만 사용한다.
- `.env.example`은 모든 지원 환경변수를 보여 주는 공개 파일로 Git에 계속 추적한다.
- 별도의 `.env.secrets.example`과 `.env.runtime.example`은 만들지 않는다.
- 실제 `.env.secrets`, `.env.runtime`, 기존 `.env`는 Git과 Docker 이미지에서 제외한다.
- `.env.secrets`에는 `DISCORD_TOKEN`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`만 둔다.
- `.env.runtime`에는 채널 ID, 경로, 백업 주기·보존 기간, `DB_BACKEND`만 둔다.
- 프로세스 환경변수가 두 파일보다 우선하고, 파일끼리는 `.env.secrets`가 `.env.runtime`보다 우선한다.
- 실제 두 파일의 권한은 `0600`으로 유지한다.
- 기존 `.env`는 두 결과 파일을 검증한 뒤에만 삭제한다.
- 비밀 값은 명령문, stdout/stderr, 보고서, Git 객체, Docker build context 또는 이미지에 노출하지 않는다.
- 새 Python 의존성은 추가하지 않는다.
- 실제 Discord 채널 ID가 제공되기 전에는 봇이나 서비스를 시작하지 않는다.

---

## File Responsibility Map

### Create locally but never track

- `.env.secrets`: 세 자격 증명만 저장한다.
- `.env.runtime`: 일반 운영 설정만 저장한다.

### Modify

- `.gitignore`: 실제 세 env 파일을 명시적으로 제외한다.
- `.dockerignore`: 실제 세 env 파일을 build context에서 제외한다.
- `.env.example`: 하나의 공개 카탈로그 안에서 secrets/runtime 섹션을 구분한다.
- `module/config.py`: 두 파일의 절대 경로와 로딩 순서를 정의한다.
- `test/console_tests.py`: 파일 분리, 우선순위, 공개 계약을 검증한다.
- `test/hello_prefix.py`: Discord 토큰을 `.env.secrets`에서 읽는다.
- `compose.yaml`: 두 서비스에 실제 두 env 파일을 주입한다.
- `README.md`: 초기 설정, 개인정보 경계, Docker, 백업 설정 안내를 갱신한다.

### Remove locally

- `.env`: 안전한 분리·검증이 끝난 뒤 삭제한다.

### Keep

- `requirements.txt`: 새 패키지를 추가하지 않는다.
- 기존 SQLite, 백업, Cog 구현: 변경하지 않는다.

---

### Task 1: Public Contract and Split Configuration Loader

**Files:**
- Modify: `.gitignore`
- Modify: `.dockerignore`
- Modify: `.env.example`
- Modify: `module/config.py`
- Modify: `test/console_tests.py`
- Modify: `test/hello_prefix.py`

**Interfaces:**
- Produces:
  - `SECRETS_ENV_FILE: pathlib.Path`
  - `RUNTIME_ENV_FILE: pathlib.Path`
  - `_load_env_files(project_root: pathlib.Path = PROJECT_ROOT) -> None`
- Preserves:
  - `PROJECT_ROOT`, `DATA_DIR`, `BACKUP_DIR`, `FORBIDDEN_WORDS_FILE`
  - `validate_config() -> None`

- [ ] **Step 1: Add failing split-loader checks**

Add these imports to `test/console_tests.py`:

```python
from dotenv import dotenv_values
```

Add:

```python
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
```

Add `import subprocess` and:

```python
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
```

Call both functions from the existing `__main__` sequence.

- [ ] **Step 2: Run the suite and confirm RED**

Run:

```bash
.venv/bin/python -m test.console_tests
```

Expected: `_load_env_files` is missing or the new ignore contract fails.

- [ ] **Step 3: Update the public and private file boundaries**

Keep `.env.example` tracked. Replace its content with:

```dotenv
# Put these values in .env.secrets
DISCORD_TOKEN=
OPENAI_API_KEY=
GOOGLE_API_KEY=

# Put these values in .env.runtime
RECRUIT_CHANNEL_ID=
EVENT_CHANNEL_ID=
DATA_DIR=runtime/data
BACKUP_DIR=runtime/backups
FORBIDDEN_WORDS_FILE=settings/forbidden_words.json
BACKUP_INTERVAL_SECONDS=21600
BACKUP_RETENTION_DAYS=30
DB_BACKEND=sqlite
```

The env section of `.gitignore` must be:

```gitignore
# Secrets and private moderation data
.env
.env.secrets
.env.runtime
settings/*.env
settings/forbidden_words.json
```

The env section of `.dockerignore` must include:

```dockerignore
.env
.env.secrets
.env.runtime
settings/*.env
settings/forbidden_words.json
```

Do not add `.env.example` to either ignore file.

- [ ] **Step 4: Implement the two-file loader**

Replace the single `.env` load in `module/config.py` with:

```python
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_ENV_FILE = PROJECT_ROOT / ".env.secrets"
RUNTIME_ENV_FILE = PROJECT_ROOT / ".env.runtime"


def _load_env_files(project_root: Path = PROJECT_ROOT) -> None:
    load_dotenv(project_root / ".env.secrets")
    load_dotenv(project_root / ".env.runtime")


_load_env_files()
```

Keep every existing setting name and `validate_config()` rule unchanged.

- [ ] **Step 5: Update the prefix test**

In `test/hello_prefix.py`, replace:

```python
load_dotenv(PROJECT_ROOT / ".env")
```

with:

```python
load_dotenv(PROJECT_ROOT / ".env.secrets")
```

- [ ] **Step 6: Run GREEN checks**

Run:

```bash
.venv/bin/python -m test.console_tests
git diff --check
git check-ignore -v .env .env.secrets .env.runtime
git ls-files --error-unmatch .env.example
```

Expected: the console suite passes, all three real files are ignored, and
`.env.example` remains tracked.

- [ ] **Step 7: Commit**

```bash
git add .gitignore .dockerignore .env.example module/config.py test/console_tests.py test/hello_prefix.py
git commit -m "feat: split secret and runtime environment files"
```

---

### Task 2: Secure Local Environment Migration

**Files:**
- Consume locally: `.env`
- Create locally: `.env.secrets`
- Create locally: `.env.runtime`
- Remove locally: `.env`
- Source changes: none

**Interfaces:**
- Consumes: Task 1's exact variable partition and ignore rules
- Produces: two mode-`0600` local files that `module.config` and Compose can use

- [ ] **Step 1: Verify safe preconditions without reading values**

Run:

```bash
test -f .env
test ! -e .env.secrets
test ! -e .env.runtime
git check-ignore -q .env
git check-ignore -q .env.secrets
git check-ignore -q .env.runtime
```

Expected: all commands exit 0.

- [ ] **Step 2: Split the legacy file without emitting values**

Run this from the repository root. It prints only status and key names on
failure, never values:

```bash
.venv/bin/python - <<'PY'
import os
import tempfile
from pathlib import Path

from dotenv import dotenv_values, set_key

root = Path.cwd()
legacy = root / ".env"
secrets_path = root / ".env.secrets"
runtime_path = root / ".env.runtime"

secret_names = (
    "DISCORD_TOKEN",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
)
runtime_names = (
    "RECRUIT_CHANNEL_ID",
    "EVENT_CHANNEL_ID",
    "DATA_DIR",
    "BACKUP_DIR",
    "FORBIDDEN_WORDS_FILE",
    "BACKUP_INTERVAL_SECONDS",
    "BACKUP_RETENTION_DAYS",
    "DB_BACKEND",
)
supported = set(secret_names + runtime_names)

if not legacy.is_file():
    raise RuntimeError("기존 .env 파일이 없습니다.")
if secrets_path.exists() or runtime_path.exists():
    raise RuntimeError("분리 대상 env 파일이 이미 존재합니다.")

values = dotenv_values(legacy)
unknown = sorted(set(values) - supported)
if unknown:
    raise RuntimeError(f"지원하지 않는 환경변수가 있습니다: {', '.join(unknown)}")

missing = sorted(name for name in supported if values.get(name) is None)
if missing:
    raise RuntimeError(f"이전할 환경변수가 없습니다: {', '.join(missing)}")


def write_file(destination: Path, names: tuple[str, ...]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=root,
        prefix=f".{destination.name}.",
        text=True,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        for name in names:
            set_key(
                str(temporary),
                name,
                values[name],
                quote_mode="always",
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


write_file(secrets_path, secret_names)
write_file(runtime_path, runtime_names)

if set(dotenv_values(secrets_path)) != set(secret_names):
    raise RuntimeError(".env.secrets 검증에 실패했습니다.")
if set(dotenv_values(runtime_path)) != set(runtime_names):
    raise RuntimeError(".env.runtime 검증에 실패했습니다.")
if (secrets_path.stat().st_mode & 0o777) != 0o600:
    raise RuntimeError(".env.secrets 권한 검증에 실패했습니다.")
if (runtime_path.stat().st_mode & 0o777) != 0o600:
    raise RuntimeError(".env.runtime 권한 검증에 실패했습니다.")

legacy.unlink()
print("환경변수 분리 완료")
PY
```

Expected: only `환경변수 분리 완료` is printed.

- [ ] **Step 3: Verify migration metadata and configuration**

Run:

```bash
test ! -e .env
test -s .env.secrets
test -s .env.runtime
test "$(stat -f '%Lp' .env.secrets)" = "600"
test "$(stat -f '%Lp' .env.runtime)" = "600"
.venv/bin/python - <<'PY'
from module import config

config.validate_config()
print("분리 설정 검증 완료")
PY
git ls-files .env .env.secrets .env.runtime
```

Expected: validation prints only its status; `git ls-files` prints nothing.

- [ ] **Step 4: Write the migration report**

The report records only:

- source file removed: boolean
- destination files present: boolean
- both modes equal `0600`: boolean
- expected key-name sets match: boolean
- `validate_config()` passes: boolean

It must not record values, prefixes, lengths, hashes, or DB/private data.

No Git commit is created because this task changes ignored local state only.

---

### Task 3: Docker Compose and Operator Runbook

**Files:**
- Modify: `compose.yaml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `.env.secrets`, `.env.runtime`
- Produces: both env files injected into `bot` and `backup`

- [ ] **Step 1: Update Compose**

For both services, replace:

```yaml
env_file:
  - .env
```

with:

```yaml
env_file:
  - .env.secrets
  - .env.runtime
```

No port, replica, volume, restart, or command changes are allowed.

- [ ] **Step 2: Update README privacy and setup**

Replace the single-file description with:

```text
.env.secrets: Discord/OpenAI/Google credentials
.env.runtime: channel IDs, paths, backup settings, SQLite backend
.env.example: tracked public catalog for both files
```

The first-time setup must create the real files explicitly:

```bash
touch .env.secrets .env.runtime
chmod 600 .env.secrets .env.runtime
```

State that operators copy the three credential names from the secrets section
of `.env.example` into `.env.secrets`, and all other names into `.env.runtime`.
Do not tell operators to copy `.env.example` wholesale into either file.

Update privacy verification to:

```bash
git check-ignore -v .env .env.secrets .env.runtime \
  settings/forbidden_words.json runtime/data/attendance_data.db
git ls-files .env .env.secrets .env.runtime \
  settings/forbidden_words.json runtime
git ls-files --error-unmatch .env.example
```

Replace references to backup settings in `.env` with `.env.runtime`.
Replace Docker explanations so both files are host-only and injected through
Compose.

Keep the existing channel-ID startup blocker explicit.

- [ ] **Step 3: Validate documentation and Compose**

Run:

```bash
docker compose config --quiet
docker compose config --services
plutil -lint deploy/macos/com.discordbot.hsr.plist.example
plutil -lint deploy/macos/com.discordbot.hsr-backup.plist.example
.venv/bin/python -m test.console_tests
git diff --check
```

Expected:

- Compose config exits 0 and reports only `backup` and `bot`.
- Both plist files report `OK`.
- Console tests pass.
- Diff check has no output.

- [ ] **Step 4: Verify the Docker image privacy boundary**

Run:

```bash
docker compose build
docker run --rm --network none --entrypoint sh discordbot-hsr-bot -c \
  'test ! -e /app/.env &&
   test ! -e /app/.env.secrets &&
   test ! -e /app/.env.runtime &&
   test ! -e /app/settings/forbidden_words.json'
```

Expected: both commands exit 0. If Compose assigns a different local image
name, obtain it with:

```bash
docker compose images --format json
```

and run the same read-only content assertion against the reported `bot` image.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml README.md
git commit -m "docs: operate with split environment files"
```

---

### Task 4: Final Split-Environment Rehearsal

**Files:**
- Verify only; no source changes expected

**Interfaces:**
- Consumes: Tasks 1-3
- Produces: evidence that the two-file boundary works locally and in Docker

- [ ] **Step 1: Run full configuration and application checks**

Run:

```bash
.venv/bin/python -m pip check
.venv/bin/python -m test.console_tests
.venv/bin/python - <<'PY'
from module import config

config.validate_config()
assert config.SECRETS_ENV_FILE.name == ".env.secrets"
assert config.RUNTIME_ENV_FILE.name == ".env.runtime"
print("분리 환경 검증 완료")
PY
```

Expected: dependencies are consistent, tests pass, and configuration validates.

- [ ] **Step 2: Verify metadata without reading values**

Run:

```bash
test ! -e .env
test -s .env.secrets
test -s .env.runtime
test "$(stat -f '%Lp' .env.secrets)" = "600"
test "$(stat -f '%Lp' .env.runtime)" = "600"
git check-ignore -q .env
git check-ignore -q .env.secrets
git check-ignore -q .env.runtime
git ls-files .env .env.secrets .env.runtime
git ls-files --error-unmatch .env.example
```

Expected: all metadata checks pass; actual files are untracked; the public
example is tracked.

- [ ] **Step 3: Verify exact key-name partition without emitting values**

Run a Python metadata check that compares only key-name sets:

```bash
.venv/bin/python - <<'PY'
from dotenv import dotenv_values

secret_names = set(dotenv_values(".env.secrets"))
runtime_names = set(dotenv_values(".env.runtime"))

assert secret_names == {
    "DISCORD_TOKEN",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
}
assert runtime_names == {
    "RECRUIT_CHANNEL_ID",
    "EVENT_CHANNEL_ID",
    "DATA_DIR",
    "BACKUP_DIR",
    "FORBIDDEN_WORDS_FILE",
    "BACKUP_INTERVAL_SECONDS",
    "BACKUP_RETENTION_DAYS",
    "DB_BACKEND",
}
assert secret_names.isdisjoint(runtime_names)
print("환경변수 이름 분리 검증 완료")
PY
```

Expected: only the status line is printed.

- [ ] **Step 4: Verify Compose and image**

Run:

```bash
docker compose config --quiet
docker compose build
```

Inspect resolved service env-file configuration without printing resolved
environment values, then repeat the image absence check for `.env`,
`.env.secrets`, `.env.runtime`, and the real forbidden-word file.

- [ ] **Step 5: Confirm scope and tracked cleanliness**

Run:

```bash
git diff main...HEAD -- requirements.txt module/database.py
git status --short --branch
git diff --check main...HEAD
```

Expected:

- no new package or database implementation change
- no tracked worktree changes
- ignored local env/runtime artifacts may remain
- branch diff is whitespace-clean

- [ ] **Step 6: Record remaining production blocker**

The rehearsal report must state:

- split env implementation: pass/fail
- public/private Git boundary: pass/fail
- Docker boundary: pass/fail
- actual channel IDs: still required before bot startup
- actual-bot-running backup rehearsal: remains pending until those IDs and
  explicit live-connect approval are supplied

No source commit is created for a clean verification-only task.

---

## Definition of Done

- `.env.example` remains tracked and documents all supported variables.
- `.env.secrets` contains only the three credentials and is ignored.
- `.env.runtime` contains only general runtime settings and is ignored.
- existing `.env` no longer exists locally.
- local process and launchd load both files through `module.config`.
- Docker Compose injects both files into both services.
- Git and Docker images contain none of the three actual env files.
- tests and Docker build pass.
- README accurately describes creation, permissions, separation, and startup blocker.
