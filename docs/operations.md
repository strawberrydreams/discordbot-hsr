# Discord Bot HSR 운영 가이드

최초 설치와 환경 설정은 [README](../README.md)를 먼저 따르세요.

이 문서의 모든 `.venv/bin/python` 명령은 README 4단계에서 만든 host virtualenv를 전제로 합니다. Docker container에서 실행하는 명령은 `docker compose run`으로 따로 표시합니다.

## 환경 설정

슬래시 명령은 전역으로 등록됩니다. 봇이 어느 서버에 초대될지 미리 알 수 없으므로 길드 한정 동기화를 쓰지 않습니다. 전역 등록은 Discord 쪽 전파에 최대 1시간이 걸릴 수 있습니다.

채널 지정은 환경변수가 아니라 서버별 설정입니다. `Administrator` 권한이 있는 각 서버 관리자는 `/설정 시작`으로 `🎮-디스코-파티` 채널을 만들고 `/설정 공지허용`으로 웹 공지 opt-in을 결정하며 `/설정 확인`으로 현재 값을 봅니다. 파티·공지·이벤트 채널과 금지어 필터 사용 여부는 봇 호스트 컴퓨터의 localhost 웹 관리에서 `guild_settings.db`에 Guild별로 저장합니다. 웹 화면의 공지 opt-in은 읽기 전용입니다. 파티 채널에는 게임 선택 패널 하나가 유지되고, 게임을 누르면 해당 게임의 활성 모집 패널이 생깁니다. 역할 버튼은 참가·변경, 별도 `나가기` 버튼은 이탈에 사용하며 마지막 인원이 나가거나 24시간이 지나면 게임 패널이 삭제됩니다. `settings/games.json`을 바꾸면 새 게임 구성과 패널을 반영하도록 봇을 재시작해야 합니다. `/이벤트` 전용 채널을 미지정하면 어느 서버 채널에서나 사용할 수 있습니다.

`/설정 시작`에는 봇의 `Manage Channels` 권한이 필요합니다.

`ADMIN_TOKEN`은 선택 사항입니다. 설정한 경우에만 웹 관리가 시작되며 32자 이상의 무작위 값이어야 합니다. Compose는 container 내부의 `0.0.0.0:8080`을 host의 `127.0.0.1:8080`에만 publish합니다. 봇을 실행한 컴퓨터의 브라우저에서 `http://127.0.0.1:8080`으로 열며, 다른 network interface에는 노출되지 않습니다. HTTP `Host`는 `127.0.0.1`과 `localhost`만 허용하고 로그인 실패 횟수도 제한합니다. 원격 접근, reverse proxy, TLS, OAuth, 길드 관리자 웹 접근은 지원하지 않으며 필요하면 별도 보안 설계를 먼저 하세요. 관리자 session cookie는 port가 아닌 host-scoped입니다. 같은 OS 사용자·loopback host trust boundary의 모든 로컬 서비스와 프로세스를 신뢰할 수 있을 때만 plain-HTTP 관리를 켜세요.

웹 관리 공지는 Discord의 `/설정 공지허용`에서 opt-in한 Guild의 지정 공지 채널에만 보냅니다. 공지당 PNG·JPEG·GIF·WebP 이미지 1개를 최대 8 MiB까지 첨부할 수 있습니다. 건너뜀·실패가 있으면 Guild별로 채널 미지정·삭제, 권한 부족, Discord 오류, 시간 초과 같은 핵심 원인을 화면에 표시합니다.

서버 간 데이터는 섞이지 않습니다. 금지어 카운트·파티·설정·게임 UID 등록은 `guild_id`로 스키마 수준에서 분리됩니다. 같은 사용자가 여러 서버에 있어도 카운트는 서버마다 독립입니다. AI 일일 한도는 예외로, 하나의 봇 인스턴스 전체에서 사용자별로 공유됩니다. DM 메시지는 귀속시킬 서버가 없어 금지어 집계에서 제외되고, 파티 패널 버튼도 서버 밖이나 최신 패널이 아닌 메시지에서는 거부됩니다.

멤버가 서버에서 나가면 해당 서버의 금지어 카운트·파티 참가·게임 UID 등록을 삭제합니다. 봇이 서버에서 추방되거나 나가면 `on_guild_remove`가 해당 서버의 모든 금지어 카운트·파티·설정·게임 UID 등록을 삭제합니다. 삭제 전 백업 사본은 기본 보존 기간인 최대 30일 뒤 제거됩니다. 다른 서버 데이터는 영향받지 않습니다.

### AI 일일 한도

| 명령 | 사용자별 KST 일일 한도 |
|---|---|
| `/기본대화` | `LIMIT_LIGHT` |
| `/고급대화` | `LIMIT_DEEP` |
| `/이미지` | `LIMIT_IMAGE` |

한도는 명령별로 적용되며, 사용자별·봇 인스턴스 전역으로 집계하고 매일 KST 자정에 리셋됩니다. `.env.runtime`의 `LIMIT_LIGHT`·`LIMIT_DEEP`·`LIMIT_IMAGE`로 각 명령의 횟수를 조정하며, `/상태`에서 오늘 남은 횟수를 확인할 수 있습니다. `AI_COOLDOWN_SECONDS`(기본 15초)는 사용자별 연속 호출 속도를 추가로 제한합니다. 날짜별 기록은 `AI_USAGE_RETENTION_DAYS`(기본 30일)보다 오래되면 다음 AI 예약 시 삭제됩니다. 값을 바꾼 뒤에는 봇을 재시작하세요.

한도는 API 호출이 시작되기 전에 예약되고, 호출 전에 실패하면 반환됩니다. provider가 호출을 수락한 뒤의 실패는 비용이 발생했을 수 있어 한도를 소비하지만, Gemini가 `429`/`RESOURCE_EXHAUSTED`로 요청 자체를 거부하면 이미지 예약을 반환합니다.

**최후의 안전망은 앱이 아니라 OpenAI 계정 예산 한도입니다.** 앱에는 전역 kill switch를 두지 않았으므로, OpenAI 대시보드에서 월 예산 상한을 반드시 설정해 두세요.

OpenAI가 `429`와 `credit_balance_exhausted`를 반환하면 모델 혼잡이 아니라 계정 크레딧 소진입니다. OpenAI API 결제 크레딧을 충전해야 `/기본대화`와 `/고급대화`가 다시 동작합니다. `/이미지`는 별도 Gemini API를 사용하며, Gemini가 `429` 또는 `RESOURCE_EXHAUSTED`를 반환하면 Google AI Studio의 할당량·요금제·결제 상태를 확인하라는 안내를 표시합니다.

이전 버전의 파티 `created_at` 중 timezone이 없는 값은 실행 방식에 따라 UTC 또는 KST로 기록됐을 수 있습니다. 마이그레이션은 조기 만료를 막기 위해 모호한 값을 UTC로 해석하므로, 일부 기존 파티는 원래 만료 시각보다 최대 9시간 더 남을 수 있지만 일찍 삭제되지는 않습니다.

파티 DB v2는 방장을 저장하고 한 사용자의 서버 내 활성 파티를 하나로 제한합니다. 업그레이드 전 중복 참가 데이터가 있으면 게임 이름 오름차순의 첫 파티만 보존하고, 기존 파티의 방장은 남은 참가자 중 가장 작은 사용자 ID로 결정합니다. 배포 전 백업 절차를 먼저 수행하세요.

### 서버 프로필 자기소개

`/프로필설정 자기소개:<문구>`로 멤버가 직접 씁니다. `/프로필`이 서버 가입일·금지어 경고 횟수와 함께 보여주고, 비어 있으면 필드 자체가 나오지 않습니다. 최대 200자이며, 인자를 비우면 삭제합니다.

`usage_data.db`의 `users` 테이블 `bio` 컬럼에 저장됩니다(스키마 v4). 기본키가 `(guild_id, user_id)`라 **서버별로 분리**되고, 멤버 퇴장·봇 추방 시 기존 삭제 경로가 함께 지웁니다. 운영자가 웹 관리에서 남의 자기소개를 대신 쓰는 기능은 두지 않았습니다.

자기소개에는 금지어 필터를 적용하지 않습니다. 봇 전역 `allowed_mentions`가 꺼져 있어 멘션은 알림을 보내지 않습니다.

### 웹 관리 화면 언어

화면은 한국어와 영어를 지원합니다. 상단 바(로그인 화면은 브랜드 패널)의 `한국어` / `English` 링크로 전환하며, 링크는 평범한 `<a href="?lang=en">`이라 JavaScript가 필요 없습니다.

언어는 다음 순서로 정해집니다.

1. `?lang=ko` 또는 `?lang=en` 쿼리
2. `admin_lang` 쿠키 (링크로 전환할 때 1년짜리로 구워집니다)
3. 브라우저의 `Accept-Language` 헤더
4. 기본값 `ko`

알 수 없는 값은 조용히 기본값으로 떨어집니다. POST 후 리다이렉트되는 화면에는 쿼리가 남지 않으므로 쿠키가 언어를 유지합니다.

문구는 `module/i18n.py`의 `STRINGS` 한 곳에 있습니다. **두 언어의 키 집합은 반드시 같아야 합니다** — 키가 빠지면 화면에 빈 문자열이 조용히 렌더되므로, 테스트가 키 집합 동등성·빈 값 없음·`{}` 자리표시자 일치·미사용 키 없음을 모두 검사합니다. 저장 결과와 오류 문구를 포함한 서버 생성 메시지도 같은 카탈로그를 씁니다.

봇 이름과 길드 이름은 번역하지 않습니다.

### 웹 관리 세션

로그인 세션은 8시간 유지되며 동시에 하나만 존재합니다(새로 로그인하면 기존 세션이 끊깁니다). 화면 우상단에 `남은 시간: 7시간 52분 (만료 21:14 KST)` 형태로 잔여 시간이 표시되고, `세션 연장` 버튼이 만료를 다시 8시간 뒤로 밉니다. 연장 횟수에는 상한이 없습니다 — 이 화면은 loopback 전용이고 세션도 하나뿐이라, 상한은 작업 중 강제 로그아웃이라는 더 나쁜 실패를 만듭니다.

**잔여 시간은 초 단위로 줄어드는 시계가 아닙니다.** 페이지를 다시 열거나 연장 버튼을 눌렀을 때 갱신됩니다. CSP가 `default-src 'self'`라 인라인 스크립트가 막혀 있고, 카운트다운 하나를 위해 CSP를 넓히지 않기로 했습니다. 웹 관리 화면은 JavaScript를 전혀 쓰지 않습니다.

화면 스타일은 `module/static/admin.css` 한 파일이며 `GET /static/admin.css`로 서빙됩니다. 로그인 화면도 이 파일을 쓰므로 인증 밖에 있고, 고정 경로 하나만 서빙하므로 경로 조작으로 다른 파일에 닿을 수 없습니다. 화면에 보이는 봇 이름은 Discord Application에 등록된 이름을 그대로 씁니다.

### 금지어 필터

필터는 localhost 웹 관리에서 길드별로 켜고 끕니다. 기본값은 켜짐이며, `guild_settings` 스키마 v6의 `forbidden_filter_enabled`에 저장됩니다. v2~v5 DB는 기동 시 자동으로 v6가 되고 기존 서버는 켜진 상태를 유지합니다. 웹에서 바꾼 값은 즉시 반영됩니다 — 필터가 메시지마다 DB를 조회하지 않도록 값을 프로세스에 캐시하고, 웹 저장이 그 캐시를 무효화합니다.

금지어가 적발될 때마다 응답과 `forbidden_count` 집계를 각각 수행합니다.

`settings/forbidden_words.json`은 단어 배열이거나 `words`·`template`·`allow`를 담은 객체입니다. `words`·`allow`는 각각 최대 1,000개·항목당 100자이고, 치환된 `template`은 2,000자 이하여야 합니다. AI persona의 system prompt는 16,000자, 인삿말은 1,974자 이하여야 합니다. 형식은 README를 참고하세요. 웹 관리에서 저장하면 들어온 형태를 그대로 유지합니다.

### 게임 카드

`game_uid_data.db`의 `game_uids` 테이블이 `(guild_id, user_id, game)`별 UID를 보관합니다. 백업·복구 경로에 다른 DB와 함께 포함됩니다.

게임 데이터 파일은 첫 조회 때 프로세스 작업 디렉터리 기준 `.enka_py/`로 내려받습니다. Compose는 이 경로를 `./runtime/enka`에 bind mount하므로 container를 다시 만들어도 재다운로드하지 않습니다. 이 디렉터리는 봇 UID로 쓰기 가능해야 합니다.

`/게임카드` 제목은 `Discord 닉네임 · 게임 이름`, 본문은 게임 닉네임·계정 레벨과 첫 진열 캐릭터·레벨의 두 줄로 표시합니다. 캐릭터 이미지는 Discord 임베드가 Enka URL에서 직접 불러옵니다.

Enka Network가 느리거나 점검 중이면 이 명령만 실패하고 다른 기능은 그대로 동작합니다. 전송 실패가 3회 연속이면 해당 게임 조회를 60초 쉬었다가 재개합니다. 조회 결과는 UID별로 5분 캐시합니다.

### 기존 설치 금지어 파일 마이그레이션

기존 금지어 파일이 있는 운영자만 봇을 중지한 뒤 목록을 한 번 복사합니다. 새 설치는 README의 복사 명령만 따릅니다.

```bash
(
set -euo pipefail
legacy_file=runtime/data/"forbidden_words.json"
target_file=settings/forbidden_words.json
test -f "$legacy_file"
if test -e "$target_file"; then
  echo "대상 파일이 이미 있습니다. 내용을 확인한 뒤 수동으로 병합하세요: $target_file" >&2
  exit 1
fi
cp -p "$legacy_file" "$target_file"
cmp -s "$legacy_file" "$target_file"
chmod 600 "$target_file"
)
```

## Docker Desktop 실행

Docker 이미지는 의존성과 `module/` 소스만 포함합니다. `.env.secrets`, `.env.runtime`, 실제 금지어, DB, 백업, 로그는 이미지에 들어가지 않습니다. 두 환경 파일은 호스트에만 두고 Compose가 `bot`과 `backup` 프로세스에 주입합니다. Compose는 중복 이름에 대해 목록의 마지막 파일을 사용하므로 `.env.runtime`, `.env.secrets` 순서로 두어 자격 증명을 우선합니다. Compose가 호스트의 `runtime/data/`와 `runtime/backups/`를 bind mount하므로 컨테이너 재생성 뒤에도 데이터와 비밀정보가 호스트에 남습니다. `bot`의 `settings` mount는 웹 관리의 원자 교체를 위해 read/write이고 `backup`의 `settings` mount는 read-only입니다. web port를 포함해 공개 포트는 없습니다.

Docker Desktop을 로그인 시 시작하도록 설정합니다. 이미지 build 뒤 non-root `bot` 사용자의 UID/GID를 bind mount 전체에 적용하고, directory는 `700`, file은 `600`으로 제한합니다. `bot`과 `backup`은 같은 이미지 사용자를 쓰므로 bot은 settings/runtime을 쓰고 backup은 read-only settings를 읽습니다. 검증이 성공해야만 서비스를 시작합니다.

```bash
docker compose config --quiet
docker compose build bot
BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
sudo chown -R "$BOT_UID:$BOT_GID" settings runtime
sudo find settings runtime -type d -exec chmod 700 {} +
sudo find settings runtime -type f -exec chmod 600 {} +
test -z "$(sudo find settings runtime \( ! -uid "$BOT_UID" -o ! -gid "$BOT_GID" -o -perm -022 \) -print -quit)"
docker compose run --rm --no-deps --entrypoint sh bot -c '
  test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
  test -r /app/settings/games.json && test -w /app/settings/games.json &&
  test -w /app/runtime/data && test -w /app/runtime/backups'
docker compose run --rm --no-deps --entrypoint sh backup -c '
  test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
docker compose up -d
docker compose logs --tail=100 bot
```

Docker Compose CLI가 없는 개발 환경에서 콘솔 suite는 rendered Compose 검사를 `SKIP`합니다. CI 또는 실제 Docker 배포 호스트에서는 `docker compose config --quiet`가 반드시 성공해야 하며 이 `SKIP`을 배포 검증으로 대신할 수 없습니다.

Mac이 잠들면 Docker Desktop 컨테이너도 중단됩니다.

## 기존 설치의 DB 파일명 이관

DB 파일 두 개의 이름이 내용에 맞게 바뀌었습니다. 봇은 새 이름만 찾으므로, 이 변경 이전부터 운영해 온 설치는 **봇을 멈춘 상태에서 한 번** 파일을 옮겨야 합니다. 옮기지 않으면 빈 DB가 새로 생성되고 기존 기록이 보이지 않습니다.

| 이전 | 이후 | 내용 |
|---|---|---|
| `attendance_data.db` | `usage_data.db` | 금지어 경고 횟수, AI 일일 사용량 (출석 기능은 이미 삭제됨) |
| `profile_data.db` | `game_uid_data.db` | 게임 UID |

```bash
docker compose stop bot backup
cd runtime/data
for suffix in "" "-wal" "-shm"; do
  test -e "attendance_data.db$suffix" && mv "attendance_data.db$suffix" "usage_data.db$suffix"
  test -e "profile_data.db$suffix" && mv "profile_data.db$suffix" "game_uid_data.db$suffix"
done
cd -
docker compose up -d bot backup
docker compose logs --tail=50 bot
```

`runtime/backups/`의 기존 백업 파일명은 그대로 두세요. manifest가 당시 이름을 기록하고 있고, 복구 절차는 manifest를 따릅니다.

## 백업 운영

수동 점검은 `backup` container에서 실행합니다.

```bash
docker compose run --rm --no-deps backup python -m module.backup create
docker compose run --rm --no-deps backup python -m module.backup verify
docker compose run --rm --no-deps backup python -m module.backup restore-test
```

`verify`와 `restore-test`는 `runtime/backups/`에서 가장 최신 manifest를 사용합니다. 각 backup set은 `usage_data.db`, `party_data.db`, `guild_settings.db`, `game_uid_data.db`와 당시 존재한 `settings/persona.json`, `settings/forbidden_words.json`, `settings/games.json`을 같은 manifest에 넣습니다. 기본 백업 주기는 21,600초(6시간), 보존 기간은 30일입니다. Docker의 `backup` 서비스는 `.env.runtime`의 `BACKUP_INTERVAL_SECONDS`, `BACKUP_RETENTION_DAYS`를 사용해 SQLite 온라인 백업을 생성합니다. 값을 변경한 뒤 `docker compose up -d --force-recreate backup`으로 백업 서비스를 다시 만드세요.

DB는 WAL 모드로 동작합니다. WAL DB는 **읽기 전용 연결이라도** `-shm`/`-wal` 파일을 만들 수 있어야 하므로, Docker `backup` 서비스의 `./runtime/data` 마운트는 `:ro`가 아니라 쓰기 가능해야 합니다. `:ro`로 되돌리면 봇이 정지한 상태(= `-shm` 부재)에서만 백업이 `attempt to write a readonly database`로 실패하고, `module.backup loop`이 예외를 삼켜 조용히 재시도만 반복합니다. 백업 코드는 SQLite 온라인 백업 API로 읽기만 하므로 rw 마운트가 데이터를 바꾸지는 않습니다.

`backup` 서비스에는 `.env.secrets`를 전달하지 않습니다. `module.backup`은 Discord·OpenAI·Google 자격증명을 사용하지 않고 `module/config.py`가 import 시점에 `validate_config()`를 부르지 않으므로, 토큰 없이도 정상 기동합니다.

`runtime/backups/`는 Time Machine 또는 외장 디스크 백업에 반드시 포함하세요. DB와 백업은 애플리케이션 수준에서 암호화하지 않으므로 host의 전체 디스크 암호화를 켜고 `0700/0600` 권한을 유지해야 합니다. 다른 host나 object storage로 복사할 때는 전송·저장 암호화와 별도 key 관리를 적용하세요. 활성 DB가 있는 `runtime/data/` 자체는 iCloud Drive, Dropbox 같은 클라우드 동기화 폴더에 두지 마세요.

## 삭제 예정 데이터 export

포인트·출석·음악 기능을 제거하는 마이그레이션은 해당 테이블과 컬럼을 되돌릴 수 없게 지웁니다. 마이그레이션이 포함된 버전으로 올리기 **전에** 아래를 실행해 사람이 읽을 수 있는 JSON으로 받아 두세요.

```bash
.venv/bin/python -m module.export_legacy
```

Docker에서는 `bot` container에서 실행합니다. 출력이 `runtime/backups/`로 떨어지므로 host에 그대로 남습니다.

```bash
docker compose run --rm --no-deps bot python -m module.export_legacy
```

출력 경로를 인자로 지정할 수도 있습니다. 기본값은 `runtime/backups/legacy-export-<UTC timestamp>.json`입니다.

내보내는 항목은 세 가지입니다.

- `users` — 길드·사용자별 포인트 잔액(`points`)과 최종 출석일(`last_attendance_date`)
- `point_ledger` — 포인트 이동 원장 전체
- `music_settings` — 길드별 음악 채널·패널 메시지 ID

원본 DB는 읽지도 쓰지도 않습니다. SQLite 온라인 백업 API로 스냅숏을 뜬 뒤 그 사본에서 읽으므로 봇이 켜져 있어도 안전하고, 결과는 특정 시점에서 일관됩니다. 이미 마이그레이션이 끝난 DB에 실행하면 사라진 항목이 `null`로 기록될 뿐 실패하지 않습니다. export는 `0600` exclusive 파일로 생성하고 기존 파일·symlink는 덮어쓰지 않습니다.

`forbidden_count`와 AI 사용량(`ai_usage`)은 삭제 대상이 아니므로 export에 포함되지 않고 마이그레이션 후에도 그대로 남습니다.

## 검증된 백업으로 실제 복구

복구는 자동화하지 않습니다. 서비스 중지 → manifest 검증과 내장 stage restore → DB 교체 → 존재한 설정 파일별 확인 교체 → 파일 권한 확인 → 서비스 시작 → health/log 확인 순서로 진행합니다. 아래 블록의 `MANIFEST`를 운영자가 선택하고, 덮어쓰기 전 비상 사본을 보관하세요. `stage_restore`가 manifest의 크기·체크섬·DB 무결성을 검증하고 stage의 복사본만 현재 스키마로 올립니다. 어느 단계든 실패하거나 복사를 거부했다면 서비스를 시작하지 마세요.

```bash
(
set -euo pipefail

MANIFEST=runtime/backups/20260728T000000Z-manifest.json

docker compose stop bot backup
running_services=$(docker compose ps --status running --services)
if grep -Eq '^(bot|backup)$' <<<"$running_services"; then
  echo "Compose 서비스가 아직 실행 중입니다." >&2
  exit 1
fi

MANIFEST_NAME=${MANIFEST##*/}
test "$MANIFEST" = "runtime/backups/$MANIFEST_NAME"
RESTORE_STAGE_NAME="restore-stage.$(date -u +%Y%m%dT%H%M%SZ).$$"
RESTORE_STAGE="runtime/data/$RESTORE_STAGE_NAME"
docker compose run --rm --no-deps -T --entrypoint python backup \
  - "/app/runtime/backups/$MANIFEST_NAME" "/app/runtime/data/$RESTORE_STAGE_NAME" <<'PY'
import sys
from pathlib import Path
from module.backup import stage_restore
stage_restore(Path(sys.argv[1]), Path(sys.argv[2]))
PY
sudo chown -R "$(id -u):$(id -g)" settings runtime

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

for name in persona.json forbidden_words.json games.json; do
  staged="$RESTORE_STAGE/settings/$name"
  test -f "$staged" || continue
  cp -ip "$staged" "settings/$name"
  cmp -s "$staged" "settings/$name"
done

SERVICE_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
SERVICE_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
sudo chown -R "$SERVICE_UID:$SERVICE_GID" settings runtime
sudo find settings runtime -type d -exec chmod 700 {} +
sudo find settings runtime -type f -exec chmod 600 {} +
test -z "$(sudo find settings runtime \( ! -uid "$SERVICE_UID" -o ! -gid "$SERVICE_GID" -o -perm -022 \) -print -quit)"
docker compose run --rm --no-deps --entrypoint sh bot -c '
  test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
  test -r /app/settings/games.json && test -w /app/settings/games.json &&
  test -w /app/runtime/data && test -w /app/runtime/backups'
docker compose run --rm --no-deps --entrypoint sh backup -c '
  test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
docker compose start backup
docker compose start bot
BOT_CONTAINER_ID=$(docker compose ps -q bot)
test -n "$BOT_CONTAINER_ID"
docker inspect --format '{{.State.Health.Status}}' "$BOT_CONTAINER_ID"
docker compose logs --tail=100 bot
)
```

이어 Discord에서 `/프로필`, `/게임카드`, 파티 채널의 게임별 패널을 확인합니다. 이상이 있으면 즉시 봇을 다시 중지하고 비상 사본과 restore stage를 보존하세요.

## 코드 린트

import 정렬 규칙은 저장소 루트의 `ruff.toml`이 정의합니다. 런타임 의존성이 아니므로 `requirements.txt`/`requirements.lock`에는 넣지 않고 `uv`로 그때그때 실행합니다.

```bash
uv tool run ruff check --select I module test          # 검사
uv tool run ruff check --select I --fix module test    # 자동 정렬
```

## 배포

변경을 모아 한 번에 배포합니다. **현재 코드로 온라인 백업 생성·검증 → pull → 테스트·빌드 → 봇·백업 재시작** 순서로 진행합니다. 이렇게 해야 새 코드의 스키마 마이그레이션 전에 구버전 DB 백업이 남습니다. 블록은 pull 전 커밋을 먼저 출력하고, 테스트나 백업 명령 하나라도 실패하면 재시작 전에 종료합니다.

각 배포·롤백 workflow는 Git 작업 전에 host virtualenv executable 존재 여부를 확인합니다.

Docker:

```bash
(
set -euo pipefail
test -x .venv/bin/python
git rev-parse HEAD
BACKUP_MANIFEST=$(docker compose run --rm --no-deps backup python -m module.backup create | tail -n 1)
test -n "$BACKUP_MANIFEST"
docker compose run --rm --no-deps backup python -c 'from pathlib import Path; from module.backup import verify_backup_set; import sys; verify_backup_set(Path(sys.argv[1]))' "$BACKUP_MANIFEST"
docker compose stop bot backup
running_services=$(docker compose ps --status running --services)
if grep -Eq '^(bot|backup)$' <<<"$running_services"; then
  echo "Compose 서비스가 아직 실행 중입니다." >&2
  exit 1
fi

BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
HOST_UID=$(id -u)
HOST_GID=$(id -g)
restore_bot_mounts() {
  sudo chown -R "$BOT_UID:$BOT_GID" settings runtime
  sudo find settings runtime -type d -exec chmod 700 {} +
  sudo find settings runtime -type f -exec chmod 600 {} +
}
verify_bot_mounts() {
  test -z "$(sudo find settings runtime \( ! -uid "$BOT_UID" -o ! -gid "$BOT_GID" -o -perm -022 \) -print -quit)"
  docker compose run --rm --no-deps --entrypoint sh bot -c '
    test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
    test -r /app/settings/games.json && test -w /app/settings/games.json &&
    test -w /app/runtime/data && test -w /app/runtime/backups'
  docker compose run --rm --no-deps --entrypoint sh backup -c '
    test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
}
trap restore_bot_mounts EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo chown -R "$HOST_UID:$HOST_GID" settings
git pull --ff-only
.venv/bin/python -m test.console_tests
docker compose config --quiet
docker compose build bot
BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
restore_bot_mounts
verify_bot_mounts
trap - EXIT HUP INT TERM
docker compose up -d --no-deps bot backup
docker compose logs --tail=100 bot
)
```

## 코드 롤백

시작 실패 시 블록이 출력한 pull 전 커밋으로 돌아갑니다. 운영자가 변경 내용을 확인한 뒤 `ROLLBACK_MODE=revert`에는 문제 커밋을, 이 호스트만 임시 복구하는 `ROLLBACK_MODE=checkout`에는 이전 커밋을 `TARGET_COMMIT`으로 넣습니다. `git reset --hard`는 사용하지 않습니다.

Docker:

```bash
(
set -euo pipefail
test -x .venv/bin/python
ROLLBACK_MODE=revert
TARGET_COMMIT=replace-with-reviewed-commit

docker compose stop bot backup
running_services=$(docker compose ps --status running --services)
if grep -Eq '^(bot|backup)$' <<<"$running_services"; then
  echo "Compose 서비스가 아직 실행 중입니다." >&2
  exit 1
fi

BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
HOST_UID=$(id -u)
HOST_GID=$(id -g)
restore_bot_mounts() {
  sudo chown -R "$BOT_UID:$BOT_GID" settings runtime
  sudo find settings runtime -type d -exec chmod 700 {} +
  sudo find settings runtime -type f -exec chmod 600 {} +
}
verify_bot_mounts() {
  test -z "$(sudo find settings runtime \( ! -uid "$BOT_UID" -o ! -gid "$BOT_GID" -o -perm -022 \) -print -quit)"
  docker compose run --rm --no-deps --entrypoint sh bot -c '
    test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
    test -r /app/settings/games.json && test -w /app/settings/games.json &&
    test -w /app/runtime/data && test -w /app/runtime/backups'
  docker compose run --rm --no-deps --entrypoint sh backup -c '
    test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
}
trap restore_bot_mounts EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo chown -R "$HOST_UID:$HOST_GID" settings
case "$ROLLBACK_MODE" in
  revert) git revert "$TARGET_COMMIT" ;;
  checkout) git checkout "$TARGET_COMMIT" ;;
  *) echo "ROLLBACK_MODE는 revert 또는 checkout이어야 합니다." >&2; exit 1 ;;
esac

.venv/bin/python -m test.console_tests
docker compose config --quiet
docker compose build bot
BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
restore_bot_mounts
verify_bot_mounts
docker compose run --rm --no-deps backup python -m module.backup verify
trap - EXIT HUP INT TERM
docker compose up -d --no-deps bot backup
)
```

DB 복구가 필요한 경우에만 위의 수동 복구 절차를 별도로 따릅니다.

## 호스트 한계

- Mac 절전은 Docker Desktop 컨테이너를 중단시킵니다.
- Docker 방식은 Docker Desktop이 로그인 시 시작되어야 합니다.
- 네트워크 또는 전원 장애가 나면 봇이 오프라인이 됩니다.
