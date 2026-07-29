# Discord Bot HSR 운영 가이드

최초 설치와 환경 설정은 [README](../README.md)를 먼저 따르세요.

> launchd와 Docker를 동시에 실행하지 마세요. 같은 Discord 토큰과 SQLite 파일을 두 프로세스가 함께 사용하게 됩니다.

## 실행 방식 선택

운영 호스트에서는 아래 방식 중 하나만 선택하세요.

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

Docker 이미지는 의존성과 `module/` 소스만 포함합니다. `.env.secrets`, `.env.runtime`, 실제 금지어, DB, 백업, 로그는 이미지에 들어가지 않습니다. 두 환경 파일은 호스트에만 두고 Compose가 `bot`과 `backup` 프로세스에 주입합니다. Compose는 중복 이름에 대해 목록의 마지막 파일을 사용하므로 `.env.runtime`, `.env.secrets` 순서로 두어 자격 증명을 우선합니다. Compose가 호스트의 `runtime/data/`, `runtime/backups/`, `settings/forbidden_words.json`을 bind mount하므로 컨테이너 재생성 뒤에도 데이터와 비밀정보가 호스트에 남습니다. 공개 포트는 없습니다.

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

`verify`와 `restore-test`는 `runtime/backups/`에서 가장 최신 manifest를 사용합니다. 기본 백업 주기는 21,600초(6시간), 보존 기간은 30일입니다. Docker 주기와 보존 기간은 `.env.runtime`의 `BACKUP_INTERVAL_SECONDS`, `BACKUP_RETENTION_DAYS`를 사용합니다. launchd 주기는 plist의 `StartInterval`을 사용합니다. 변경 시 설치된 백업 plist도 수정하고, 이미 로드된 job을 `launchctl bootout --wait gui/$(id -u)/com.discordbot.hsr-backup`으로 내린 뒤 `launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"`으로 다시 등록해야 합니다. launchd의 별도 백업 LaunchAgent와 Docker의 `backup` 서비스는 SQLite 온라인 백업을 생성합니다.

`runtime/backups/`는 Time Machine 또는 외장 디스크 백업에 반드시 포함하세요. 활성 DB가 있는 `runtime/data/` 자체는 iCloud Drive, Dropbox 같은 클라우드 동기화 폴더에 두지 마세요.

## 검증된 백업으로 실제 복구

복구는 자동화하지 않습니다. 아래 블록의 `DEPLOYMENT`와 `MANIFEST`를 운영자가 직접 선택한 뒤 **블록 전체를 한 번에** 실행하고, `cp -i`가 묻는 두 덮어쓰기를 각각 승인합니다. 어느 단계든 실패하거나 덮어쓰기를 거부해 파일이 선택한 백업과 다르면 봇을 시작하지 않습니다.

```bash
(
set -euo pipefail

DEPLOYMENT=launchd
MANIFEST=runtime/backups/20260728T000000Z-manifest.json

case "$DEPLOYMENT" in
  launchd)
    launchd_domain="gui/$(id -u)"
    stop_launchd_job() {
      launchd_target="$launchd_domain/$1"
      if launchctl print "$launchd_target" >/dev/null 2>&1; then
        launchctl bootout --wait "$launchd_target"
      fi
      if launchctl print "$launchd_target" >/dev/null 2>&1; then
        echo "launchd job이 아직 실행 중입니다: $launchd_target" >&2
        return 1
      fi
    }
    stop_launchd_job com.discordbot.hsr
    stop_launchd_job com.discordbot.hsr-backup
    ;;
  docker)
    docker compose stop bot backup
    running_services=$(docker compose ps --status running --services)
    if grep -Eq '^(bot|backup)$' <<<"$running_services"; then
      echo "Compose 서비스가 아직 실행 중입니다." >&2
      exit 1
    fi
    ;;
  *)
    echo "DEPLOYMENT는 launchd 또는 docker여야 합니다." >&2
    exit 1
    ;;
esac

test -f "$MANIFEST"
RESTORE_STAGE=$(mktemp -d runtime/restore-stage.XXXXXX)
.venv/bin/python - "$MANIFEST" "$RESTORE_STAGE" <<'PY'
import json
import shutil
import sys
from pathlib import Path

from module.backup import restore_test

manifest_path = Path(sys.argv[1])
stage = Path(sys.argv[2])
document = json.loads(manifest_path.read_text(encoding="utf-8"))
items = document.get("databases")
expected = {"attendance_data.db", "party_data.db"}
entries = {}
backup_names = set()

if not isinstance(items, list):
    raise RuntimeError("manifest databases가 목록이 아닙니다.")
for item in items:
    if not isinstance(item, dict):
        raise RuntimeError("잘못된 manifest DB 항목입니다.")
    source = item.get("source")
    backup = item.get("backup")
    if (
        not isinstance(source, str)
        or source not in expected
        or source in entries
        or not isinstance(backup, str)
        or not backup
        or Path(backup).name != backup
        or backup in backup_names
    ):
        raise RuntimeError("안전하지 않거나 중복된 manifest DB 항목입니다.")
    entries[source] = manifest_path.parent / backup
    backup_names.add(backup)
if set(entries) != expected:
    raise RuntimeError("manifest에 두 운영 DB가 정확히 한 번씩 있어야 합니다.")

restore_test(manifest_path)
for source, backup_path in entries.items():
    shutil.copy2(backup_path, stage / source)
PY

EMERGENCY_DIR=$(mktemp -d "runtime/emergency.$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
cp -p runtime/data/attendance_data.db runtime/data/party_data.db "$EMERGENCY_DIR"/
cmp -s runtime/data/attendance_data.db "$EMERGENCY_DIR/attendance_data.db"
cmp -s runtime/data/party_data.db "$EMERGENCY_DIR/party_data.db"
ls -l "$EMERGENCY_DIR" "$RESTORE_STAGE"

cp -ip "$RESTORE_STAGE/attendance_data.db" runtime/data/attendance_data.db
cp -ip "$RESTORE_STAGE/party_data.db" runtime/data/party_data.db
cmp -s "$RESTORE_STAGE/attendance_data.db" runtime/data/attendance_data.db
cmp -s "$RESTORE_STAGE/party_data.db" runtime/data/party_data.db

.venv/bin/python -c 'from module.backup import DATABASES, verify_database; from module.config import DATA_DIR; [print(name, verify_database(DATA_DIR / name, tables)) for name, tables in DATABASES.items()]'

case "$DEPLOYMENT" in
  launchd)
    launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"
    launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr.plist"
    ;;
  docker)
    docker compose start backup
    docker compose start bot
    ;;
esac
)
```

로그에 startup 오류가 없는지 확인하고 Discord에서 `/지갑`, `/랭킹`, `/파티`, `/프로필`을 읽어 포인트·순위·파티·금지어 경고 횟수를 확인합니다. 이상이 있으면 즉시 봇을 다시 중지하고 비상 사본과 restore stage를 보존하세요.

## 배포

변경을 모아 한 번에 배포합니다. 아래 순서를 바꾸지 않습니다: **테스트 → 온라인 백업 생성 → 백업 검증 → 봇 1회 재시작**. 각 블록은 pull 전 커밋을 먼저 출력하고, 테스트나 백업 명령 하나라도 실패하면 재시작 전에 종료합니다.

macOS:

```bash
(
set -euo pipefail
git rev-parse HEAD
git pull --ff-only
.venv/bin/python -m test.console_tests
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr
tail -n 100 runtime/logs/bot.log
)
```

Docker:

```bash
(
set -euo pipefail
git rev-parse HEAD
git pull --ff-only
.venv/bin/python -m test.console_tests
BACKUP_MANIFEST=$(docker compose run --rm --no-deps backup python -m module.backup create | tail -n 1)
test -n "$BACKUP_MANIFEST"
docker compose run --rm --no-deps backup python -c 'from pathlib import Path; from module.backup import verify_backup_set; import sys; verify_backup_set(Path(sys.argv[1]))' "$BACKUP_MANIFEST"
docker compose build bot backup
docker compose run --rm --no-deps backup python -c 'from pathlib import Path; from module.backup import verify_backup_set; import sys; verify_backup_set(Path(sys.argv[1]))' "$BACKUP_MANIFEST"
docker compose up -d --no-deps bot backup
docker compose logs --tail=100 bot
)
```

## 코드 롤백

시작 실패 시 블록이 출력한 pull 전 커밋으로 돌아갑니다. 운영자가 변경 내용을 확인한 뒤 `ROLLBACK_MODE=revert`에는 문제 커밋을, 이 호스트만 임시 복구하는 `ROLLBACK_MODE=checkout`에는 이전 커밋을 `TARGET_COMMIT`으로 넣습니다. `git reset --hard`는 사용하지 않습니다.

macOS:

```bash
(
set -euo pipefail
ROLLBACK_MODE=revert
TARGET_COMMIT=replace-with-reviewed-commit

case "$ROLLBACK_MODE" in
  revert) git revert "$TARGET_COMMIT" ;;
  checkout) git checkout "$TARGET_COMMIT" ;;
  *) echo "ROLLBACK_MODE는 revert 또는 checkout이어야 합니다." >&2; exit 1 ;;
esac

.venv/bin/python -m test.console_tests
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr
)
```

Docker:

```bash
(
set -euo pipefail
ROLLBACK_MODE=revert
TARGET_COMMIT=replace-with-reviewed-commit

case "$ROLLBACK_MODE" in
  revert) git revert "$TARGET_COMMIT" ;;
  checkout) git checkout "$TARGET_COMMIT" ;;
  *) echo "ROLLBACK_MODE는 revert 또는 checkout이어야 합니다." >&2; exit 1 ;;
esac

.venv/bin/python -m test.console_tests
docker compose build bot backup
docker compose run --rm --no-deps backup python -m module.backup verify
docker compose up -d --no-deps bot backup
)
```

DB 복구가 필요한 경우에만 위의 수동 복구 절차를 별도로 따릅니다.

## 호스트 한계

- Mac 절전은 LaunchAgent와 Docker Desktop 컨테이너를 모두 중단시킵니다.
- Docker 방식은 Docker Desktop이 로그인 시 시작되어야 합니다.
- LaunchAgent는 로그인한 사용자 세션에서 실행됩니다.
- 네트워크 또는 전원 장애가 나면 어떤 방식이든 봇이 오프라인이 됩니다.
