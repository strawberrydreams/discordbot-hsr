# Discord Bot HSR 운영 가이드

최초 설치와 환경 설정은 [README](../README.md)를 먼저 따르세요.

> launchd와 Docker를 동시에 실행하지 마세요. 같은 Discord 토큰과 SQLite 파일을 두 프로세스가 함께 사용하게 됩니다.

## 환경 설정

슬래시 명령은 전역으로 등록됩니다. 봇이 어느 서버에 초대될지 미리 알 수 없으므로 길드 한정 동기화를 쓰지 않습니다. 전역 등록은 Discord 쪽 전파에 최대 1시간이 걸릴 수 있습니다.

채널 지정은 환경변수가 아니라 서버별 설정입니다. 각 서버 관리자가 `/설정 모집채널`, `/설정 이벤트채널`로 지정하며 값은 `guild_settings.db`에 저장됩니다. 미지정 상태에서는 `/모집`·`/파티`·`/나가기`·`/변경`·`/이벤트`가 안내 메시지와 함께 잠깁니다. `/설정 확인`으로 현재 값을 볼 수 있습니다.

서버 간 데이터는 섞이지 않습니다. 포인트·출석·금지어 카운트·파티·설정은 `guild_id`로 스키마 수준에서 분리됩니다. 같은 사용자가 여러 서버에 있어도 잔액은 서버마다 독립입니다. AI 일일 한도는 예외로, 하나의 봇 인스턴스 전체에서 사용자별로 공유됩니다. DM 메시지는 귀속시킬 서버가 없어 금지어 집계에서 제외되고, 파티 참가 버튼도 서버 밖에서는 거부됩니다.

봇이 서버에서 추방되거나 나가면 `on_guild_remove`가 해당 서버의 포인트·원장·파티·설정을 삭제합니다. 다른 서버 데이터는 영향받지 않습니다.

### 포인트 경제

포인트의 유일한 수입원은 `/출석`입니다(하루 5,000~30,000 P, 평균 17,500 P). 통화를 발행하는 다른 경로를 추가하지 마세요 — 발행처가 둘이면 아래 가격의 근거가 무너집니다. 이 이유로 `/럭키박스`는 제거했습니다.

| 명령 | 가격 | 사용자별 KST 일일 한도 | 상수 |
|---|---|---|---|
| `/기본대화` | 200 P | `LIMIT_LIGHT` | `hyacine_chat_cog.LIGHT_COST` |
| `/고급대화` | 2,000 P | `LIMIT_DEEP` | `hyacine_chat_cog.DEEP_COST` |
| `/이미지` | 30,000 P | `LIMIT_IMAGE` | `hyacine_image_cog.IMAGE_COST` |

가격을 조정할 때는 구현 계획(`docs/superpowers/plans/2026-07-30-followup-hardening.md`)의 「포인트 경제 근거」 절을 함께 갱신하세요.

일일 한도는 포인트와 별도로 적용되며, 사용자별·봇 인스턴스 전역으로 집계하고 매일 KST 자정에 리셋됩니다. `.env.runtime`의 `LIMIT_LIGHT`·`LIMIT_DEEP`·`LIMIT_IMAGE`로 각 명령의 횟수를 조정하며, `/상태`에서 오늘 남은 횟수를 확인할 수 있습니다. `AI_COOLDOWN_SECONDS`(기본 15초)는 사용자별 연속 호출 속도를 추가로 제한합니다. 값을 바꾼 뒤에는 봇을 재시작하세요.

**최후의 안전망은 앱이 아니라 OpenAI 계정 예산 한도입니다.** 앱에는 전역 kill switch를 두지 않았으므로, OpenAI 대시보드에서 월 예산 상한을 반드시 설정해 두세요.

### 포인트 원장으로 환불 실패 대조하기

모든 포인트 이동은 `attendance_data.db`의 `point_ledger` 테이블에 append-only로 기록됩니다(`delta`, `reason`, `created_at` epoch 초). 실패한 차감은 기록되지 않습니다. 사용자가 "자동 환불에 실패했습니다. 관리자에게 수동 정산을 요청해 주세요" 안내를 받았다면 다음으로 대조합니다.

```bash
.venv/bin/python -c '
from module.database import create_attendance_repository
import datetime
GUILD_ID = 000000000000000000  # 대상 서버 ID로 교체
USER_ID = 000000000000000000  # 대상 사용자 ID로 교체
repo = create_attendance_repository()
for delta, reason, at in repo.get_ledger(GUILD_ID, USER_ID, limit=50):
    print(datetime.datetime.fromtimestamp(at), f"{delta:+,}", reason)
print("잔액:", repo.get_points(GUILD_ID, USER_ID))'
```

`chat:<model>` / `image` 차감에 짝이 되는 `chat_refund:<model>` / `image_refund` 행이 없으면 환불이 누락된 것입니다. 원장 합계는 항상 현재 잔액과 일치해야 합니다.

이전 버전의 파티 `created_at` 중 timezone이 없는 값은 Docker가 UTC, launchd가 KST로 기록했을 수 있습니다. 마이그레이션은 조기 만료를 막기 위해 모호한 값을 UTC로 해석합니다. 기존 launchd 파티는 원래 만료 시각보다 최대 9시간 더 남을 수 있지만 일찍 삭제되지는 않습니다.

### 기존 설치 금지어 파일 마이그레이션

기존 금지어 파일이 있는 운영자만 봇을 중지한 뒤 목록을 한 번 복사합니다. 새 설치는 README의 복사 명령만 따릅니다.

```bash
legacy_file=runtime/data/"forbidden_words.json"
cp "$legacy_file" settings/forbidden_words.json
chmod 600 settings/forbidden_words.json
```

## 실행 방식 선택

운영 호스트에서는 아래 방식 중 하나만 선택하세요.

### macOS LaunchAgent

제공된 템플릿은 현재 저장소 경로와 로그인 사용자의 그룹을 자동으로 넣어 설치용 파일을 만듭니다. newsyslog 설정 형식은 경로의 공백을 지원하지 않으므로 저장소는 공백이 없는 경로에 복제해야 합니다.

최초 설치:

```bash
mkdir -p "$HOME/Library/LaunchAgents" runtime/data runtime/backups runtime/logs
.venv/bin/python deploy/macos/render_templates.py
cp runtime/generated/macos/com.discordbot.hsr.plist "$HOME/Library/LaunchAgents/"
cp runtime/generated/macos/com.discordbot.hsr-backup.plist "$HOME/Library/LaunchAgents/"
sudo cp runtime/generated/macos/com.discordbot.hsr.conf /etc/newsyslog.d/
plutil -lint "$HOME/Library/LaunchAgents/com.discordbot.hsr.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr-backup
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr
```

기존의 주기 실행 방식 백업 job을 설치한 호스트는 아래 1회 업그레이드를 먼저 수행합니다. job이 없으면 `launchctl print` 조건이 bootout을 건너뛰며, 다른 실패는 `set -e`로 즉시 중단됩니다.

```bash
(
set -euo pipefail
launchd_domain="gui/$(id -u)"
backup_target="$launchd_domain/com.discordbot.hsr-backup"
backup_plist="$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"

if launchctl print "$backup_target" >/dev/null 2>&1; then
  launchctl bootout --wait "$backup_target"
fi
.venv/bin/python deploy/macos/render_templates.py
cp runtime/generated/macos/com.discordbot.hsr-backup.plist "$backup_plist"
plutil -lint "$backup_plist"
launchctl bootstrap "$launchd_domain" "$backup_plist"
launchctl kickstart -k "$backup_target"
)
```

로그 로테이션 설치와 구문 확인:

```bash
sudo newsyslog -nvv
```

네 newsyslog 항목은 봇의 `DATA_DIR/.bot.pid` 또는 백업의 `BACKUP_DIR/.backup.pid`를 읽어 SIGHUP(1)을 보냅니다. PID와 companion lock은 launchd로 실행하는 macOS에서만 생성되며 Docker에서는 만들지 않습니다. SIGHUP 뒤 남은 PID는 launchd 재시작이 lock을 다시 획득한 뒤 원자적으로 교체합니다. 기본 SIGHUP 종료 뒤 launchd의 `KeepAlive`가 해당 LaunchAgent를 재시작해 새 로그 파일을 다시 엽니다. 따라서 rotation 때 해당 프로세스가 잠시 재시작됩니다.

상태와 로그:

```bash
launchctl print gui/$(id -u)/com.discordbot.hsr
tail -n 100 runtime/logs/bot.log
tail -n 100 runtime/logs/bot-error.log
```

LaunchAgent는 로그인한 사용자 세션에서만 실행됩니다. Mac이 잠들면 봇과 예약 백업도 중단됩니다.

### Docker Desktop

Docker 이미지는 의존성과 `module/` 소스만 포함합니다. `.env.secrets`, `.env.runtime`, 실제 금지어, DB, 백업, 로그는 이미지에 들어가지 않습니다. 두 환경 파일은 호스트에만 두고 Compose가 `bot`과 `backup` 프로세스에 주입합니다. Compose는 중복 이름에 대해 목록의 마지막 파일을 사용하므로 `.env.runtime`, `.env.secrets` 순서로 두어 자격 증명을 우선합니다. Compose가 호스트의 `runtime/data/`와 `runtime/backups/`를 bind mount하므로 컨테이너 재생성 뒤에도 데이터와 비밀정보가 호스트에 남습니다. 공개 포트는 없습니다.

Docker Desktop을 로그인 시 시작하도록 설정한 뒤 최초 실행:

```bash
docker compose config --quiet
docker compose build bot
docker compose up -d
docker compose logs --tail=100 bot
```

Docker Compose CLI가 없는 개발 환경에서 콘솔 suite는 rendered Compose 검사만 `SKIP`하고 plist/newsyslog 검사를 계속합니다. CI 또는 실제 Docker 배포 호스트에서는 `docker compose config --quiet`가 반드시 성공해야 하며 이 `SKIP`을 배포 검증으로 대신할 수 없습니다.

Mac이 잠들면 Docker Desktop 컨테이너도 중단됩니다.

## 백업 운영

수동 점검:

```bash
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
```

`verify`와 `restore-test`는 `runtime/backups/`에서 가장 최신 manifest를 사용합니다. 기본 백업 주기는 21,600초(6시간), 보존 기간은 30일입니다. Docker와 launchd 모두 `.env.runtime`의 `BACKUP_INTERVAL_SECONDS`, `BACKUP_RETENTION_DAYS`를 사용합니다. 값을 변경한 뒤 백업 LaunchAgent를 `launchctl bootout --wait gui/$(id -u)/com.discordbot.hsr-backup`으로 내리고 `launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.discordbot.hsr-backup.plist"`으로 다시 등록합니다. launchd의 별도 백업 LaunchAgent와 Docker의 `backup` 서비스는 SQLite 온라인 백업을 생성합니다.

DB는 WAL 모드로 동작합니다. WAL DB는 **읽기 전용 연결이라도** `-shm`/`-wal` 파일을 만들 수 있어야 하므로, Docker `backup` 서비스의 `./runtime/data` 마운트는 `:ro`가 아니라 쓰기 가능해야 합니다. `:ro`로 되돌리면 봇이 정지한 상태(= `-shm` 부재)에서만 백업이 `attempt to write a readonly database`로 실패하고, `module.backup loop`이 예외를 삼켜 조용히 재시도만 반복합니다. 백업 코드는 SQLite 온라인 백업 API로 읽기만 하므로 rw 마운트가 데이터를 바꾸지는 않습니다.

`backup` 서비스에는 `.env.secrets`를 전달하지 않습니다. `module.backup`은 Discord·OpenAI·Google 자격증명을 사용하지 않고 `module/config.py`가 import 시점에 `validate_config()`를 부르지 않으므로, 토큰 없이도 정상 기동합니다.

`runtime/backups/`는 Time Machine 또는 외장 디스크 백업에 반드시 포함하세요. 활성 DB가 있는 `runtime/data/` 자체는 iCloud Drive, Dropbox 같은 클라우드 동기화 폴더에 두지 마세요.

## 검증된 백업으로 실제 복구

복구는 자동화하지 않습니다. 아래 블록의 `DEPLOYMENT`와 `MANIFEST`를 운영자가 직접 선택한 뒤 **블록 전체를 한 번에** 실행하고, `cp -i`가 묻는 각 DB 덮어쓰기를 승인합니다. 어느 단계든 실패하거나 덮어쓰기를 거부해 파일이 선택한 백업과 다르면 봇을 시작하지 않습니다.

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

from module.backup import DATABASES, restore_test

manifest_path = Path(sys.argv[1])
stage = Path(sys.argv[2])
document = json.loads(manifest_path.read_text(encoding="utf-8"))
items = document.get("databases")
expected = set(DATABASES)
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
    raise RuntimeError("manifest에 운영 DB가 정확히 한 번씩 있어야 합니다.")

restore_test(manifest_path)
for source, backup_path in entries.items():
    shutil.copy2(backup_path, stage / source)
PY

EMERGENCY_DIR=$(mktemp -d "runtime/emergency.$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
for staged in "$RESTORE_STAGE"/*.db; do
  name=${staged##*/}
  cp -p "runtime/data/$name" "$EMERGENCY_DIR/$name"
  cmp -s "runtime/data/$name" "$EMERGENCY_DIR/$name"
done
ls -l "$EMERGENCY_DIR" "$RESTORE_STAGE"

for staged in "$RESTORE_STAGE"/*.db; do
  name=${staged##*/}
  cp -ip "$staged" "runtime/data/$name"
  cmp -s "$staged" "runtime/data/$name"
done

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

변경을 모아 한 번에 배포합니다. macOS는 **테스트 → 온라인 백업 생성 → 백업 검증 → 봇·백업 재시작**, Docker는 **테스트 → 이미지 빌드 → 온라인 백업 생성 → 백업 검증 → 봇·백업 재시작** 순서로 진행합니다. 각 블록은 pull 전 커밋을 먼저 출력하고, 테스트나 백업 명령 하나라도 실패하면 재시작 전에 종료합니다.

macOS:

```bash
(
set -euo pipefail
git rev-parse HEAD
git pull --ff-only
.venv/bin/python -m test.console_tests
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr-backup
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
docker compose config --quiet
docker compose build bot
BACKUP_MANIFEST=$(docker compose run --rm --no-deps backup python -m module.backup create | tail -n 1)
test -n "$BACKUP_MANIFEST"
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
launchctl kickstart -k gui/$(id -u)/com.discordbot.hsr-backup
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
docker compose config --quiet
docker compose build bot
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
