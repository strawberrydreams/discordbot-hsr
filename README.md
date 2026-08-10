# Hyacine — Discord Bot HSR

Hyacine은 한국어 Discord 서버용 **자가 호스팅 커뮤니티 유틸리티 봇**입니다. 출석·포인트, 파티 모집, 서버 이벤트, 금융 조회, 금지어 관리, GPT 대화와 이미지 생성을 제공합니다. Python 3.12 이상과 SQLite를 사용합니다.

각 운영자가 자신의 Discord Application과 토큰을 만들어 직접 운영합니다. 저장소 소유자는 다른 사람에게 봇을 호스팅하거나 Discord 서버를 대신 관리하지 않습니다. Hyacine은 *Honkai: Star Rail* 비공식 팬 프로젝트이며 HoYoverse와 제휴하거나 승인받지 않았습니다.

포인트·출석·금지어 카운트, 파티, 길드 설정 데이터는 `guild_id`로 서버별 분리됩니다. AI 사용량 한도만 사용자별·봇 인스턴스 전역으로 공유되는 명시적 예외입니다.

## 주요 기능

- `/출석`, `/지갑`, `/프로필`, `/랭킹`
- 게임별 영속 파티 패널 — 파티 채널에서 버튼으로 모집·참가·역할 변경·나가기
- 어느 서버 채널에서나 `/이벤트`
- `/기본대화`, `/고급대화`, `/이미지`
- `/주가`와 금지어 경고
- `/설정` — `Manage Guild` 권한이 있는 서버 관리자가 파티·음악 채널과 파티 호스트 공지 수신을 설정
- 선택 사항인 음악 패널 — URL을 대기열에 넣어 음성 채널에서 재생

## 빠른 시작

### 1. Discord Application 생성과 설치

각 운영자는 Discord Developer Portal에서 자신의 Discord Application과 봇 토큰을 생성합니다. Installation은 **Guild Install만** 사용하고, 설치 scope는 `bot`, `applications.commands`로 설정합니다. 봇 권한은 다음만 부여합니다.

- `View Channel`
- `Send Messages`
- `Read Message History`
- `Embed Links`
- `Attach Files`
- `Manage Channels` — `/설정 시작`이 봇 전용 category와 파티·음악 채널을 만들 때 필요
- `Connect`
- `Speak`

Bot 설정에서 privileged intent인 `Message Content`와 `Server Members`를 활성화합니다. 전자는 금지어 필터에, 후자는 퇴장 시 파티 정리에 필요합니다. 운영자 전용 앱은 `Public Bot`을 끄세요. 이는 Portal에서 하는 설정이며, 이 저장소의 코드가 Portal 설정을 변경하지는 않습니다.

슬래시 명령은 전역으로 등록되므로 초대 직후 반영까지 최대 1시간이 걸릴 수 있습니다. 봇을 서버에서 내보내면 해당 서버의 포인트·파티·설정은 자동으로 삭제됩니다.

### 2. 설치

```bash
touch .env.secrets .env.runtime
mkdir -p runtime/data runtime/backups runtime/logs
cp settings/persona.example.json settings/persona.json
cp settings/forbidden_words.example.json settings/forbidden_words.json
cp settings/games.example.json settings/games.json
chmod 600 .env.secrets .env.runtime settings/persona.json settings/forbidden_words.json settings/games.json
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

### 3. 환경과 서버 설정

`.env.example`을 기준으로 다음 두 파일을 채웁니다.

- `.env.secrets`: 방금 만든 Discord 토큰과 OpenAI·Google 자격 증명
- `.env.runtime`: 데이터·백업 경로, 백업 주기, AI 쿨다운과 한도

채널 ID는 환경변수가 아닙니다. 봇을 Guild에 설치한 뒤 서버 관리자가 `/설정 시작`으로 봇 전용 category와 파티·음악 채널을 만들거나 `/설정 파티채널`, `/설정 음악채널`로 기존 채널을 지정합니다. `/설정 공지허용`은 이 Guild의 파티 호스트 공지 수신만 바꿉니다.

파티 채널에는 `settings/games.json`의 게임별 영속 패널이 하나씩 유지됩니다. 빈 자리 버튼은 참가, 자신의 자리 버튼은 퇴장, 다른 역할 버튼은 역할 변경입니다. 게임·역할 설정을 바꾼 뒤에는 봇을 재시작하세요.

음악 패널의 `추가`를 누른 뒤 개별 HTTP(S) URL을 넣고, 요청자는 봇과 같은 음성 채널에 있어야 합니다. `건너뛰기`와 대기열 제거는 요청자 또는 서버 관리자가 할 수 있고, `일시정지`·`정지`는 서버 관리자만 할 수 있습니다. 재생목록은 지원하지 않습니다.

실제 금지어 목록은 `settings/forbidden_words.json`에 작성합니다. 이 목록은 한 봇 인스턴스에 공통 적용되며, 경고 횟수는 서버별로 집계됩니다.

음악의 `PyNaCl` 또는 `yt-dlp` 의존성이 없으면 음악 extension만 건너뛰고 나머지 봇은 계속 동작합니다. 외부 사이트 변경으로 yt-dlp가 깨질 수 있으며, 직접 실행은 `.venv/bin/python -m pip install --upgrade yt-dlp` 후 재시작하고 Docker는 `docker compose build --no-cache bot && docker compose up -d --no-deps bot`으로 새 이미지를 만드세요. 콘텐츠 저작권과 해당 서비스 약관 준수는 운영자 책임이며, 어떤 사이트나 콘텐츠의 재생을 보장하지 않습니다.

`ADMIN_TOKEN`은 선택 사항입니다. 비어 있으면 웹 관리를 시작하지 않습니다. 설정하면 웹 관리는 고정된 `127.0.0.1:8080`에만 bind됩니다. Docker Compose는 이 포트를 publish하지 않으므로 컨테이너 밖에서 접근할 수 없습니다. 원격 접근, reverse proxy, TLS, OAuth, 길드 관리자 웹 접근은 지원하지 않습니다. 필요하다면 별도 보안 설계를 먼저 하세요.

AI 명령은 포인트 비용과 **별도로** 사용자별 KST 일일 AI 한도를 적용합니다. `.env.runtime`의 `LIMIT_LIGHT`, `LIMIT_DEEP`, `LIMIT_IMAGE`로 각각 조정하며 매일 KST 자정에 리셋됩니다. 이 한도는 같은 봇 인스턴스 안에서 모든 Guild에 걸쳐 사용자별로 공유됩니다. 앱 한도와 별개로 OpenAI·Google provider 계정에도 예산 상한을 설정하세요.

### 4. DB 초기화와 초기 백업

새 설치에서만 빈 DB를 초기화합니다. 기존 운영 DB가 있다면 초기화하지 말고 [운영 가이드](docs/operations.md)의 복구 절차를 따르세요.

```bash
.venv/bin/python -c 'from module.database import create_attendance_repository, create_party_repository, create_guild_settings_repository; from module.backup import DATABASES, verify_database; from module.config import DATA_DIR; create_attendance_repository(); create_party_repository(); create_guild_settings_repository(); [print(name, verify_database(DATA_DIR / name, tables, source_name=name)) for name, tables in DATABASES.items()]'
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
```

### 5. Docker Compose로 배포 (권장)

일반 운영에는 Docker Compose를 권장합니다. 배포 순서는 테스트, Compose 설정 확인, 이미지 빌드, 봇·백업 서비스 시작입니다.

```bash
.venv/bin/python -m unittest test.test_discord_commands
.venv/bin/python -m test.console_tests
docker compose config --quiet
docker compose build bot
docker compose up -d
docker compose logs --tail=100 bot
```

개발 중 직접 실행하려면 `.venv/bin/python -m module.main`을 사용합니다. macOS에서 Docker 대신 LaunchAgent를 직접 관리해야 하는 고급 운영자만 [launchd 선택 사항](docs/operations.md#macos-launchagent)을 따르세요. launchd와 Docker를 동시에 실행하지 마세요.

## 운영 시 주의

- `.env.secrets`, `.env.runtime`, `runtime/`은 Git에 추가하지 마세요. `git add -f`도 사용하지 않습니다.
- 운영 백엔드는 SQLite만 지원합니다.
- 포인트 잔액은 서버별로 독립입니다. A 서버에서 번 포인트를 B 서버에서 쓸 수 없습니다.
- plain HTTP 관리자 세션 cookie는 포트가 아니라 host에 묶입니다. localhost 웹 관리를 켠 OS 사용자/loopback host 경계의 모든 로컬 서비스·프로세스를 신뢰할 수 있을 때만 `ADMIN_TOKEN`을 설정하세요.

## 라이선스와 기여

Hyacine은 [MIT License](LICENSE)로 배포합니다. 이 저장소는 사용자 기여를 받지 않습니다.

## 상세 운영

Docker Compose·launchd 실행, 백업, 복구, 배포, 롤백은 [운영 가이드](docs/operations.md)를 참고하세요.
