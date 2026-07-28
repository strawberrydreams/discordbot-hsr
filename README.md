# Discord Bot HSR 운영 런북

개인 디스코드 서버용 봇입니다. 운영 백엔드는 **SQLite만 지원**합니다. 모든 DB SQL은 `module/database.py`의 Repository 구현에 모여 있고 Cog는 Repository 인터페이스를 사용합니다. 이 내부 경계는 기능 코드와 저장 코드를 분리하기 위한 것이며, MySQL·Oracle 같은 외부 DB 배포는 구현되어 있지 않습니다.

## 공개 저장소와 로컬 비밀정보

이 저장소는 공개 상태로 운영할 수 있습니다. `.env`, 실제 `settings/forbidden_words.json`, `runtime/`의 DB·백업·로그는 Git에서 추적하지 않으며 Docker 이미지에도 포함되지 않습니다. 실제 비밀 값은 운영 호스트에만 둡니다.

다음 파일은 예제에서 로컬로 복사해 편집하고, 어떤 경우에도 `git add -f`로 강제 추가하지 마세요.

- `.env`: Discord/OpenAI/Google 키, 서버 ID, 런타임 경로
- `settings/forbidden_words.json`: 실제 금지어 목록

확인:

```bash
git check-ignore -v .env settings/forbidden_words.json runtime/data/attendance_data.db
git ls-files .env settings/forbidden_words.json runtime
```

두 번째 명령은 아무것도 출력하지 않아야 합니다.

## 최초 설치

저장소 루트에서 실행합니다. Python 3.11 이상이 필요합니다.

```bash
cp .env.example .env
cp settings/forbidden_words.example.json settings/forbidden_words.json
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m test.console_tests
```

`.env`의 빈 키와 ID, 실제 금지어 목록을 로컬에서 편집한 뒤 런타임 디렉터리를 만듭니다.

```bash
mkdir -p runtime/data runtime/backups runtime/logs
```

봇은 기존 DB를 자동 복구하거나 새로 만들지 않고, Cog를 로드하기 전에 무결성을 검사합니다. 따라서 최초 1회에 한해 Repository 팩토리로 두 DB를 생성하거나 기존 스키마를 마이그레이션하고, 같은 프로세스에서 `module.backup.verify_database`로 검사합니다.

```bash
.venv/bin/python -c 'from module.database import create_attendance_repository, create_party_repository; from module.backup import DATABASES, verify_database; from module.config import DATA_DIR; create_attendance_repository(); create_party_repository(); [print(name, verify_database(DATA_DIR / name, tables)) for name, tables in DATABASES.items()]'
```

이 명령은 **초기화 전용**이며 백업에서 데이터를 복원하지 않습니다. 기존 운영 DB가 있다면 먼저 아래 복구 절차로 검증·복원하고, 빈 DB로 덮어쓰지 마세요.

초기 백업도 만들고 검사합니다.

```bash
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
```

## 실행 방식 선택

**launchd와 Docker를 동시에 실행하면 안 됩니다.** 같은 Discord 토큰과 SQLite 파일을 두 프로세스가 함께 사용하게 됩니다. 운영 호스트에서는 아래 방식 중 하나만 선택하세요.

### macOS LaunchAgent

제공된 plist는 이 저장소의 현재 절대 경로(`/Users/strawberrydreams/coding/discordbot-hsr`)를 사용합니다. 다른 위치에 복제했다면 두 템플릿의 Python, 작업 디렉터리, 로그 경로를 먼저 수정합니다.

최초 설치:

```bash
mkdir -p "$HOME/Library/LaunchAgents" runtime/logs
cp deploy/macos/com.discordbot.hsr.plist.example "$HOME/Library/LaunchAgents/com.discordbot.hsr.plist"
cp deploy/macos/com.discordbot.hsr-backup.plist.example "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.discordbot.hsr.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr
```

상태와 로그:

```bash
launchctl print gui/$(id -u)/com.discordbot.hsr
tail -n 100 runtime/logs/bot.log
tail -n 100 runtime/logs/bot-error.log
```

LaunchAgent는 로그인한 사용자 세션에서만 실행됩니다. Mac이 잠들면 봇과 예약 백업도 중단됩니다.

### Docker Desktop

Docker 이미지는 의존성과 `module/` 소스만 포함합니다. `.env`, 실제 금지어, DB, 백업, 로그는 이미지에 들어가지 않습니다. Compose가 호스트의 `runtime/data/`, `runtime/backups/`, `settings/forbidden_words.json`을 bind mount하므로 컨테이너 재생성 뒤에도 데이터와 비밀정보가 호스트에 남습니다. 공개 포트는 없습니다.

Docker Desktop을 로그인 시 시작하도록 설정한 뒤 최초 실행:

```bash
docker compose build
docker compose up -d
docker compose logs --tail=100 bot
```

Mac이 잠들면 Docker Desktop 컨테이너도 중단됩니다.

## 백업 운영

수동 점검:

```bash
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
```

`verify`와 `restore-test`는 `runtime/backups/`에서 가장 최신 manifest를 사용합니다. 기본 백업 주기는 21,600초(6시간), 보존 기간은 30일입니다. Docker 주기와 보존 기간은 `.env`의 `BACKUP_INTERVAL_SECONDS`, `BACKUP_RETENTION_DAYS`를 사용합니다. launchd 주기는 plist의 `StartInterval`을 사용하므로 변경 시 설치된 plist도 수정하고 다시 bootstrap해야 합니다. launchd의 별도 백업 LaunchAgent와 Docker의 `backup` 서비스는 SQLite 온라인 백업을 생성합니다.

`runtime/backups/`는 Time Machine 또는 외장 디스크 백업에 반드시 포함하세요. 활성 DB가 있는 `runtime/data/` 자체는 iCloud Drive, Dropbox 같은 클라우드 동기화 폴더에 두지 마세요.

## 검증된 백업으로 실제 복구

복구는 자동화하지 않습니다. 운영자가 manifest와 각 덮어쓰기를 직접 확인해야 합니다.

1. 실행 방식에 맞게 봇을 반드시 중지합니다. 복구 중간 상태가 예약 백업되지 않도록 백업 서비스도 함께 중지합니다.

```bash
# launchd
launchctl bootout gui/$(id -u)/com.discordbot.hsr
launchctl bootout gui/$(id -u)/com.discordbot.hsr-backup

# 또는 Docker
docker compose stop bot
docker compose stop backup
```

2. 복구할 manifest를 직접 선택하고, 그 백업을 임시 디렉터리에 복사해 검사합니다.

```bash
ls -1 runtime/backups/*-manifest.json
MANIFEST=runtime/backups/20260728T000000Z-manifest.json
.venv/bin/python -c 'from pathlib import Path; from module.backup import restore_test; import sys; restore_test(Path(sys.argv[1]))' "$MANIFEST"
```

3. 현재 live DB 두 개를 타임스탬프가 붙은 비상 디렉터리에 보존합니다.

```bash
EMERGENCY_DIR="runtime/emergency-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$EMERGENCY_DIR"
cp -p runtime/data/attendance_data.db runtime/data/party_data.db "$EMERGENCY_DIR"/
ls -l "$EMERGENCY_DIR"
```

4. 선택한 백업의 타임스탬프와 파일명을 확인한 뒤, `cp -i`가 묻는 각 덮어쓰기에 운영자가 승인합니다.

```bash
STAMP=${MANIFEST##*/}
STAMP=${STAMP%-manifest.json}
ls -l "runtime/backups/${STAMP}-attendance_data.db" "runtime/backups/${STAMP}-party_data.db"
cp -ip "runtime/backups/${STAMP}-attendance_data.db" runtime/data/attendance_data.db
cp -ip "runtime/backups/${STAMP}-party_data.db" runtime/data/party_data.db
```

5. 복원된 live DB에 `PRAGMA integrity_check`와 필수 테이블 검사를 실행합니다.

```bash
.venv/bin/python -c 'from module.backup import DATABASES, verify_database; from module.config import DATA_DIR; [print(name, verify_database(DATA_DIR / name, tables)) for name, tables in DATABASES.items()]'
```

6. 봇을 시작합니다.

```bash
# launchd
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"

# 또는 Docker
docker compose start bot
docker compose start backup
```

7. 로그에 startup 오류가 없는지 확인하고 Discord에서 `/지갑`, `/랭킹`, `/파티`, `/프로필`을 읽어 포인트·순위·파티·금지어 경고 횟수를 확인합니다. 이상이 있으면 즉시 봇을 다시 중지하고 비상 사본을 보존하세요.

## 배포

변경을 모아 한 번에 배포합니다. 먼저 pull 전 커밋을 별도로 기록합니다.

```bash
git rev-parse HEAD
```

아래 순서를 바꾸지 않습니다: **테스트 → 온라인 백업 생성 → 백업 검증 → 봇 1회 재시작**. 테스트나 백업 명령 하나라도 실패하면 즉시 중단하며 재시작하지 않습니다.

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

## 코드 롤백

시작 실패 시 pull 전에 기록한 커밋으로 돌아갑니다. 운영자가 변경 내용을 확인한 뒤 공유 브랜치라면 정상 `git revert <문제-커밋>`, 이 호스트만 임시 복구한다면 승인한 이전 커밋으로 `git checkout <이전-커밋>`을 사용합니다. `git reset --hard`는 사용하지 않습니다.

코드를 되돌린 뒤 테스트를 다시 통과시키고, 선택한 실행 방식으로 한 번만 재시작합니다.

```bash
.venv/bin/python -m test.console_tests

# macOS
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr

# 또는 Docker
docker compose build bot
docker compose up -d --no-deps bot
```

DB 복구가 필요한 경우에만 위의 수동 복구 절차를 별도로 따릅니다.

## 호스트 한계

- Mac 절전은 LaunchAgent와 Docker Desktop 컨테이너를 모두 중단시킵니다.
- Docker 방식은 Docker Desktop이 로그인 시 시작되어야 합니다.
- LaunchAgent는 로그인한 사용자 세션에서 실행됩니다.
- 네트워크 또는 전원 장애가 나면 어떤 방식이든 봇이 오프라인이 됩니다.
