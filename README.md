# Hyacine — Discord Bot HSR

Hyacine은 한국어 Discord 서버용 **자가 호스팅 커뮤니티 유틸리티 봇**입니다. 파티 모집, 서버 이벤트, 금지어 관리, GPT 대화와 이미지 생성을 제공합니다. Python 3.12 이상과 SQLite를 사용합니다.

각 운영자가 자신의 Discord Application과 토큰을 만들어 직접 운영합니다. 저장소 소유자는 다른 사람에게 봇을 호스팅하거나 Discord 서버를 대신 관리하지 않습니다. Hyacine은 *Honkai: Star Rail* 비공식 팬 프로젝트이며 HoYoverse와 제휴하거나 승인받지 않았습니다.

금지어 카운트, 파티, 길드 설정 데이터는 `guild_id`로 서버별 분리됩니다. AI 사용량 한도만 사용자별·봇 인스턴스 전역으로 공유되는 명시적 예외입니다.

## 주요 기능

- `/프로필` — 서버 가입일과 금지어 경고 횟수
- 게임별 영속 파티 패널 — 파티 채널에서 버튼으로 모집·참가·역할 변경·나가기
- 어느 서버 채널에서나 `/이벤트`
- `/기본대화`, `/고급대화`, `/이미지`
- 금지어 경고 — 서버별로 끌 수 있고, 응답 문구와 예외 목록을 설정할 수 있습니다
- `/인사` — AI 키 없이도 동작하는 정적 인삿말
- `/설정` — `Manage Guild` 권한이 있는 서버 관리자가 파티 채널, 파티 호스트 공지 수신, 금지어 필터 사용 여부를 설정

## 빠른 시작

### 1. Discord Application과 토큰 준비

Discord Developer Portal에서 자신의 Discord Application과 봇 토큰을 생성합니다. Bot 설정에서 privileged intent인 `Message Content`와 `Server Members`를 활성화합니다. 전자는 금지어 필터에, 후자는 퇴장 시 파티 정리에 필요합니다. 운영자 전용 앱은 `Public Bot`을 끄세요. 이는 Portal에서 하는 설정이며, 이 저장소의 코드가 Portal 설정을 변경하지는 않습니다. Guild 설치는 봇을 시작한 뒤 6단계에서 합니다.

### 2. 환경 파일 작성

`.env.example`을 기준으로 두 파일을 만들고, 다음 단계로 가기 전에 값을 채웁니다.

- `.env.secrets`: 방금 만든 Discord 토큰과 선택적인 OpenAI·Google 자격 증명
- `.env.runtime`: 데이터·백업 경로, 백업 주기, AI 쿨다운과 한도

```bash
touch .env.secrets .env.runtime
chmod 600 .env.secrets .env.runtime
```

### 3. 설정 파일 초기화

세 example을 모두 복사합니다.

```bash
cp settings/persona.example.json settings/persona.json
cp settings/forbidden_words.example.json settings/forbidden_words.json
cp settings/games.example.json settings/games.json
mkdir -p runtime/data runtime/backups runtime/logs
```

### 4. host test 환경과 이미지 빌드

배포·롤백 전에 실행할 console suite용 host virtualenv를 먼저 설치한 뒤 이미지를 build합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
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

설치 후 `Manage Guild` 권한이 있는 서버 관리자가 Discord에서 `/설정 시작`을 실행합니다. 채널 ID는 환경변수가 아니며, `/설정 파티채널`로 기존 채널을 지정할 수도 있습니다. `/설정 공지허용`은 이 Guild의 파티 호스트 공지 수신만, `/설정 금지어`는 이 Guild의 금지어 필터 사용 여부만 바꿉니다. `/설정 확인`으로 현재 상태를 볼 수 있습니다.

슬래시 명령은 전역으로 등록되므로 초대 직후 반영까지 최대 1시간이 걸릴 수 있습니다. 봇을 서버에서 내보내면 해당 서버의 금지어 카운트·파티·설정은 자동으로 삭제됩니다.

### 패널과 선택 기능

파티 채널에는 `settings/games.json`의 게임별 영속 패널이 하나씩 유지됩니다. 빈 자리 버튼은 참가, 자신의 자리 버튼은 퇴장, 다른 역할 버튼은 역할 변경입니다. 게임·역할 설정을 바꾼 뒤에는 봇을 재시작하세요.

#### 금지어 필터

실제 금지어 목록은 `settings/forbidden_words.json`에 작성합니다. 이 목록은 한 봇 인스턴스에 공통 적용되며, 경고 횟수는 서버별로 집계됩니다.

**설치 전에 이 동작을 확인하세요.** 지인끼리 쓰는 서버에서는 농담이지만 모르는 사람이 있는 서버에서는 다르게 받아들여질 수 있습니다.

- 원본 메시지를 **삭제하지 않습니다.** 봇이 같은 채널에 응답을 하나 더 보냅니다.
- 걸린 단어를 **응답 본문에 그대로 노출합니다.**
- 작성자를 멘션합니다. 다만 봇 전역으로 멘션 알림이 꺼져 있어 **표시만 되고 알림은 가지 않습니다.**
- 봇이 보낸 메시지는 검사하지 않습니다. 웹훅 메시지도 마찬가지입니다.
- 같은 채널에서 연달아 걸리면 응답은 10초에 한 번만 나갑니다. 경고 횟수는 그 사이에도 전부 집계됩니다.
- `Manage Guild` 권한이 있는 서버 관리자가 `/설정 금지어 사용:False`로 이 서버에서만 끌 수 있습니다. 기본값은 켜짐입니다.

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

`ADMIN_TOKEN`은 선택 사항입니다. 비어 있으면 웹 관리를 시작하지 않습니다. 설정하면 웹 관리는 고정된 `127.0.0.1:8080`에만 bind됩니다. Docker Compose는 이 포트를 host의 `127.0.0.1`에만 publish하므로 봇을 실행한 컴퓨터의 브라우저에서 `http://127.0.0.1:8080`으로 열 수 있고, 다른 network interface에는 노출되지 않습니다. 원격 접근, reverse proxy, TLS, OAuth, 길드 관리자 웹 접근은 지원하지 않습니다. 필요하다면 별도 보안 설계를 먼저 하세요.

웹 관리에서는 AI 페르소나(system prompt와 인삿말), 금지어 목록, 파티 게임 목록을 편집합니다. 금지어는 저장 즉시 다시 불러오고, 페르소나는 새로 시작하는 AI 채널 세션부터, 게임 목록은 봇 재시작 후 반영됩니다. 같은 화면에서 Guild별 파티 채널과 공지 opt-in 상태를 확인하고, opt-in한 Guild에 공지를 보낼 수 있습니다.

AI 명령은 사용자별 KST 일일 AI 한도를 명령별로 적용합니다. `.env.runtime`의 `LIMIT_LIGHT`, `LIMIT_DEEP`, `LIMIT_IMAGE`로 각각 조정하며 매일 KST 자정에 리셋됩니다. 이 한도는 같은 봇 인스턴스 안에서 모든 Guild에 걸쳐 사용자별로 공유됩니다. 앱 한도와 별개로 OpenAI·Google provider 계정에도 예산 상한을 설정하세요.

일반 운영에는 Docker Compose를 권장합니다. macOS에서 Docker 대신 LaunchAgent를 직접 관리해야 하는 고급 운영자만 [launchd 선택 사항](docs/operations.md#macos-launchagent)을 따르세요. launchd와 Docker를 동시에 실행하지 마세요. 백업·복구 절차도 [운영 가이드](docs/operations.md)를 따르세요.

## 운영 시 주의

- `.env.secrets`, `.env.runtime`, `runtime/`은 Git에 추가하지 마세요. `git add -f`도 사용하지 않습니다.
- 운영 백엔드는 SQLite만 지원합니다.
- plain HTTP 관리자 세션 cookie는 포트가 아니라 host에 묶입니다. localhost 웹 관리를 켠 OS 사용자/loopback host 경계의 모든 로컬 서비스·프로세스를 신뢰할 수 있을 때만 `ADMIN_TOKEN`을 설정하세요.

## 라이선스와 기여

Hyacine은 [MIT License](LICENSE)로 배포합니다. 이 저장소는 사용자 기여를 받지 않습니다.

## 상세 운영

Docker Compose·launchd 실행, 백업, 복구, 배포, 롤백은 [운영 가이드](docs/operations.md)를 참고하세요.
