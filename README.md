# Hyacine — Discord Bot HSR

Hyacine은 한국어 Discord 서버용 **자가 호스팅 커뮤니티 유틸리티 봇**입니다. 파티 모집, 서버 이벤트, 금지어 관리, GPT 대화와 이미지 생성, 게임 프로필 카드를 제공합니다. Python 3.12 이상과 SQLite를 사용합니다. macOS·Linux 같은 **POSIX 호스트**에서 동작하며, Windows에서는 WSL2 위에서 실행합니다(아래 [실행 환경](#실행-환경) 참고).

각 운영자가 자신의 Discord Application과 토큰을 만들어 직접 운영합니다. 저장소 소유자는 다른 사람에게 봇을 호스팅하거나 Discord 서버를 대신 관리하지 않습니다. Hyacine은 *Honkai: Star Rail* 비공식 팬 프로젝트이며 HoYoverse와 제휴하거나 승인받지 않았습니다.

금지어 카운트, 파티, 길드 설정, 게임 UID 등록 데이터는 `guild_id`로 서버별 분리됩니다. AI 사용량 한도만 사용자별·봇 인스턴스 전역으로 공유되는 명시적 예외입니다.

## 주요 기능

- `/프로필` — 서버 가입일과 금지어 경고 횟수
- 게임 선택 패널 — `🎮-디스코-파티`에서 게임을 고르면 해당 게임의 모집 패널 생성
- `/이벤트` — 웹 관리에서 서버별 전용 채널을 선택할 수 있고, 미지정 시 모든 채널에서 사용
- `/기본대화`, `/고급대화`, `/이미지`
- 금지어 경고 — 서버별로 끌 수 있고, 응답 문구와 예외 목록을 설정할 수 있습니다
- `/인사` — AI 키 없이도 동작하는 정적 인삿말
- `/등록`, `/등록해제`, `/게임카드` — 원신 · 붕괴: 스타레일 · 젠레스 존 제로의 첫 진열 캐릭터 카드
- `/설정 시작`, `/설정 공지허용`, `/설정 확인` — `Administrator` 권한이 있는 서버 관리자가 기본 파티 채널을 만들고 웹 공지 수신 동의를 결정하며 현재 설정을 확인

## 빠른 시작

### 실행 환경

아래 설치·실행 명령은 **POSIX 호스트**를 전제로 합니다. **macOS·Linux 터미널**에서는 그대로 동작하고, 프로덕션은 어느 OS든 **Docker Compose**로 운영합니다.

**Windows에서는 WSL2 위에서 실행합니다.** 봇 코드가 Unix 전용 기능(파일 락·소유자 권한 검사·원자적 파일 교체)을 사용하므로 네이티브 Windows Python으로는 실행되지 않습니다. 다음 두 가지 중 하나를 쓰세요.

- **Docker Desktop (WSL2 백엔드)** — 아래 4·5단계의 Docker Compose 절차를 그대로 사용합니다. 컨테이너가 Linux라 코드 수정이 필요 없습니다. Docker Desktop 설치 시 WSL2 통합을 켜세요.
- **WSL2 배포판(Ubuntu 등) 셸** — WSL2 셸을 열고 아래 모든 단계를 macOS·Linux와 동일하게 실행합니다. host virtualenv(`.venv`)로 봇을 직접 실행할 때도 이 셸을 씁니다.

어느 방법이든 **리포지토리를 WSL 리눅스 파일시스템 안(예: `~/discordbot-hsr`)에 clone하세요.** Windows 쪽 경로(`/mnt/c/…`)에 두면 `chmod`·소유권·bind mount 권한이 리눅스 방식으로 매핑되지 않아, env 파일 권한 검사와 Compose 5단계의 소유권 설정이 실패합니다.

### 1. Discord Application과 토큰 준비

Discord Developer Portal에서 자신의 Discord Application과 봇 토큰을 생성합니다. Bot 설정에서 privileged intent인 `Message Content`와 `Server Members`를 활성화합니다. 전자는 금지어 필터에, 후자는 퇴장 시 파티 참가·금지어 카운트·게임 UID 정리에 필요합니다. 운영자 전용 앱은 `Public Bot`을 끄세요. 이는 Portal에서 하는 설정이며, 이 저장소의 코드가 Portal 설정을 변경하지는 않습니다. Guild 설치는 봇을 시작한 뒤 6단계에서 합니다.

### 2. 환경 파일 작성

`.env.example`을 기준으로 두 파일을 만들고, 다음 단계로 가기 전에 값을 채웁니다.

- `.env.secrets`: 방금 만든 Discord 토큰과 선택적인 OpenAI·Google 자격 증명
- `.env.runtime`: 데이터·백업 경로, 백업 주기, AI 쿨다운과 한도

웹 관리를 켤 때 `ADMIN_TOKEN`은 아래처럼 32바이트 무작위 값으로 생성해
`.env.secrets`에 넣습니다. 32자보다 짧으면 봇이 기동하지 않습니다.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```bash
touch .env.secrets .env.runtime
chmod 600 .env.secrets .env.runtime
```

봇은 두 파일이 현재 사용자 소유의 일반 파일이고 group/world 권한이 없는지
기동 전에 확인하며, symlink나 느슨한 권한이면 비밀값을 읽지 않고 종료합니다.

### 3. 설정 파일 초기화

세 example을 모두 복사합니다.

```bash
cp settings/persona.example.json settings/persona.json
cp settings/forbidden_words.example.json settings/forbidden_words.json
cp settings/games.example.json settings/games.json
mkdir -p runtime/data runtime/backups runtime/logs runtime/enka
```

### 4. host test 환경과 이미지 빌드

배포·롤백 전에 실행할 console suite용 host virtualenv를 먼저 설치한 뒤 이미지를 build합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -r requirements-audit.txt
.venv/bin/python -m pip_audit -r requirements.lock
docker compose config --quiet
docker compose build bot
```

### 5. bind mount 소유권·권한 설정 후 시작

이미지의 non-root `bot` UID/GID를 호스트 bind mount에 적용합니다. `bot`과 `backup`은 같은 이미지를 사용하므로 이 소유권으로 bot은 settings/runtime을 쓰고 backup은 read-only settings를 읽을 수 있습니다. 아래 검증이 모두 성공하기 전에는 서비스를 시작하지 마세요.

```bash
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

### 6. Guild 설치와 Discord 설정

Developer Portal의 Installation은 **Guild Install만** 사용하고 scope는 `bot`, `applications.commands`로 설정합니다. 봇 권한은 다음만 부여한 뒤 Guild에 설치합니다.

- `View Channel`
- `Send Messages`
- `Read Message History`
- `Embed Links`
- `Attach Files`
- `Manage Channels` — 봇 전용 category와 파티 채널 생성

설치 후 `Administrator` 권한이 있는 서버 관리자가 Discord에서 `/설정 시작`을 실행합니다. 채널 ID는 환경변수가 아니며, 파티·공지·이벤트 채널과 금지어 필터 사용 여부는 봇 호스트 컴퓨터의 localhost 웹 관리에서 Guild별로 저장합니다. 웹 공지 수신 동의는 서버 관리자가 Discord의 `/설정 공지허용`에서만 변경하며, `/설정 확인`으로 현재 상태를 볼 수 있습니다.

슬래시 명령은 전역으로 등록되므로 초대 직후 반영까지 최대 1시간이 걸릴 수 있습니다. 멤버가 서버를 나가면 그 서버에 속한 금지어 카운트·파티 참가·게임 UID 등록을 삭제하고, 봇을 서버에서 내보내면 해당 서버의 금지어 카운트·파티·설정·게임 UID 등록을 모두 삭제합니다. 삭제 전 백업 사본은 기본 보존 기간인 최대 30일 뒤 제거됩니다.

### 패널과 선택 기능

`🎮-디스코-파티` 채널에는 처음에 게임 선택 패널 하나만 표시됩니다. 게임 버튼을 누른 사용자가 방장이 되고 해당 게임의 편성 패널이 생깁니다. 역할 버튼은 참가·역할 변경에, 별도 `나가기` 버튼은 파티 이탈에 사용합니다. 마지막 인원이 나가거나 24시간이 지나면 해당 게임 패널이 사라집니다. 게임·역할 설정을 바꾼 뒤에는 봇을 재시작하세요.

#### 금지어 필터

실제 금지어 목록은 `settings/forbidden_words.json`에 작성합니다. 이 목록은 한 봇 인스턴스에 공통 적용되며, 경고 횟수는 서버별로 집계됩니다.

**설치 전에 이 동작을 확인하세요.** 지인끼리 쓰는 서버에서는 농담이지만 모르는 사람이 있는 서버에서는 다르게 받아들여질 수 있습니다.

- 원본 메시지를 **삭제하지 않습니다.** 봇이 같은 채널에 응답을 하나 더 보냅니다.
- 걸린 단어를 **응답 본문에 그대로 노출합니다.**
- 작성자를 멘션합니다. 다만 봇 전역으로 멘션 알림이 꺼져 있어 **표시만 되고 알림은 가지 않습니다.**
- 봇이 보낸 메시지는 검사하지 않습니다. 웹훅 메시지도 마찬가지입니다.
- 금지어가 걸릴 때마다 응답하고 경고 횟수를 집계합니다.
- 봇 호스트 운영자가 localhost 웹 관리에서 Guild별로 끌 수 있습니다. 기본값은 켜짐입니다.

문서는 단어 배열이거나, 응답 문구와 예외를 함께 담은 객체일 수 있습니다. 기존 배열 파일은 그대로 동작하므로 바꿀 필요가 없습니다.

```json
{
  "words": ["금지어"],
  "template": "🛑 {mention} 님, {word} 는 금지입니다.",
  "allow": ["금지어가 들어간 멀쩡한 표현"]
}
```

- `template`에서 치환되는 것은 `{mention}`과 `{word}` 둘뿐입니다. 다른 중괄호는 문자 그대로 남습니다. 생략하면 기본 문구를 씁니다.
- 매칭은 공백·기호를 지운 뒤의 부분 일치라 오탐이 납니다. `allow`에 적힌 표현 안에서 걸린 적발은 무시합니다. 기본값은 비어 있으니 겪은 오탐을 그때 추가하세요.
- `words`와 `allow`는 각각 최대 1,000개, 정규화된 항목당 최대 100자입니다. `template`은 가장 긴 멘션과 단어를 치환한 결과가 Discord의 2,000자 한도 안이어야 합니다.

#### 게임 카드

`/등록 <게임> <UID>`로 계정을 등록하면 `/게임카드 <게임>`가 제목에 Discord 닉네임과 게임 이름을, 본문에 게임 닉네임·계정 레벨과 첫 진열 캐릭터·레벨을 두 줄로 표시합니다. 데이터는 [Enka Network](https://enka.network)에서 가져옵니다.

- UID 등록은 **서버별로 따로**입니다. 한 서버에 등록해도 다른 서버에는 자동으로 적용되지 않고, 어느 서버에 등록했는지가 서버를 넘어 보이지도 않습니다.
- 등록 시점에 Enka로 실존을 확인하므로, 없는 UID는 등록되지 않습니다.
- Enka는 **게임 안에서 진열장에 올린 캐릭터만** 돌려줍니다. UID가 맞아도 진열장이 비어 있으면 카드가 비는데, 이때는 게임별 설정 경로를 안내합니다.
- 응답은 UID별로 몇 분간 캐시합니다. Enka가 느리거나 점검 중이면 해당 명령만 실패하고 봇의 나머지 기능은 그대로 동작합니다.
- 카드는 Discord 임베드가 Enka의 첫 캐릭터 이미지를 직접 표시합니다.
- 게임 데이터 파일은 첫 조회 때 `.enka_py/`(Compose에서는 `runtime/enka/`)로 내려받습니다. 이 디렉터리가 쓰기 가능해야 합니다.

Hyacine은 HoYoverse와 제휴하지 않았고 Enka Network와도 무관합니다.

`ADMIN_TOKEN`은 선택 사항입니다. 비어 있으면 웹 관리를 시작하지 않으며, 설정할 때는 32자 이상의 무작위 값이어야 합니다. host에서 직접 실행하면 웹 관리는 `127.0.0.1:8080`에 bind됩니다. Docker Compose에서는 container 내부의 `0.0.0.0:8080`에서 받고 host의 `127.0.0.1:8080`에만 publish하므로, 어느 실행 방식이든 봇을 실행한 컴퓨터의 브라우저에서 `http://127.0.0.1:8080`으로 열 수 있고 다른 network interface에는 노출되지 않습니다. HTTP `Host`도 `127.0.0.1`과 `localhost`만 허용하고 로그인 실패는 제한합니다. 원격 접근, reverse proxy, TLS, OAuth, 길드 관리자 웹 접근은 지원하지 않습니다. 필요하다면 별도 보안 설계를 먼저 하세요.

웹 관리에서는 AI 페르소나(system prompt와 인삿말), 금지어 목록, 파티 게임 목록을 편집합니다. system prompt는 최대 16,000자, 인삿말은 최대 1,974자입니다. 금지어는 저장 즉시 다시 불러오고, 페르소나는 새로 시작하는 AI 채널 세션부터, 게임 목록은 봇 재시작 후 반영됩니다. 같은 화면에서 Guild별 파티·공지·이벤트 채널과 금지어 필터 사용 여부를 필드별로 저장하고 웹 공지 opt-in은 읽기 전용으로 표시합니다. 공지는 Discord에서 opt-in한 Guild의 지정 채널로 임베드와 선택 이미지 1개(최대 8 MiB)를 보낼 수 있고, 작성 폼 안의 Discord Markdown 문법 가이드를 참고할 수 있습니다. 전송을 건너뛰거나 실패하면 결과와 함께 Guild별 핵심 원인을 표시합니다.

AI 명령은 사용자별 KST 일일 AI 한도를 명령별로 적용합니다. `.env.runtime`의 `LIMIT_LIGHT`, `LIMIT_DEEP`, `LIMIT_IMAGE`로 각각 조정하며 매일 KST 자정에 리셋됩니다. 이 한도는 같은 봇 인스턴스 안에서 모든 Guild에 걸쳐 사용자별로 공유됩니다. 날짜별 사용 기록은 `AI_USAGE_RETENTION_DAYS`(기본 30일) 뒤 다음 AI 예약 시 삭제됩니다. 앱 한도와 별개로 OpenAI·Google provider 계정에도 예산 상한을 설정하세요.

OpenAI가 `credit_balance_exhausted`를 반환하면 `/기본대화`와 `/고급대화`는 운영자에게 API 크레딧 충전이 필요하다고 안내합니다. `/이미지`에서 Google Gemini API가 `429` 또는 `RESOURCE_EXHAUSTED`를 반환하면 할당량 또는 결제 한도를 확인하라는 별도 안내를 표시합니다.

프로덕션은 Docker Compose로 운영합니다. 백업·복구 절차는 [운영 가이드](docs/operations.md)를 따르세요.

## 운영 시 주의

- `.env.secrets`, `.env.runtime`, `runtime/`은 Git에 추가하지 마세요. `git add -f`도 사용하지 않습니다.
- 운영 백엔드는 SQLite만 지원합니다.
- `settings/`와 `runtime/`은 소유자 전용 권한으로 유지하고 host의 전체 디스크 암호화를 켜세요. 백업을 다른 host나 object storage로 옮길 때는 전송·저장 암호화를 별도로 적용하세요.
- plain HTTP 관리자 세션 cookie는 포트가 아니라 host에 묶입니다. localhost 웹 관리를 켠 OS 사용자/loopback host 경계의 모든 로컬 서비스·프로세스를 신뢰할 수 있을 때만 `ADMIN_TOKEN`을 설정하세요.

## 라이선스와 기여

Hyacine은 [GNU General Public License v3.0](LICENSE)으로 배포합니다. 이 저장소는 사용자 기여를 받지 않습니다.

프로필 카드가 쓰는 [`enka`](https://pypi.org/project/enka/)가 GPL-3.0이므로, 이 라이브러리를 링크하는 배포물 전체가 GPL-3.0이 됩니다. MIT였던 이전 버전을 쓰던 운영자는 해당 커밋 이전 이력에서 MIT 조건으로 계속 쓸 수 있습니다. 포크해서 배포한다면 소스 공개 의무를 포함한 GPL-3.0 조건을 따라야 합니다.

## 상세 운영

Docker Compose 실행, 백업, 복구, 배포, 롤백은 [운영 가이드](docs/operations.md)를 참고하세요.
