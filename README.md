# Hyacine — Discord Bot HSR

Hyacine은 한국어 Discord 서버용 **자가 호스팅 커뮤니티 유틸리티 봇**입니다. 출석·포인트, 파티 모집, 서버 이벤트, 금융 조회, 금지어 관리, GPT 대화와 이미지 생성을 제공합니다. Python 3.12 이상과 SQLite를 사용합니다.

각 운영자가 자신의 Discord Application과 토큰을 만들어 직접 운영합니다. 저장소 소유자는 다른 사람에게 봇을 호스팅하거나 Discord 서버를 대신 관리하지 않습니다. Hyacine은 *Honkai: Star Rail* 비공식 팬 프로젝트이며 HoYoverse와 제휴하거나 승인받지 않았습니다.

포인트·출석·금지어 카운트, 파티, 길드 설정 데이터는 `guild_id`로 서버별 분리됩니다. AI 사용량 한도만 사용자별·봇 인스턴스 전역으로 공유되는 명시적 예외입니다.

## 주요 기능

- `/출석`, `/지갑`, `/프로필`, `/랭킹`
- `/모집`, `/파티`, `/나가기`, `/변경`
- 지정 채널의 `/이벤트`
- `/기본대화`, `/고급대화`, `/이미지`
- `/주가`와 금지어 경고
- `/설정` — `Manage Guild` 권한이 있는 서버 관리자가 모집·이벤트 채널을 지정

## 빠른 시작

### 1. Discord Application 생성과 설치

각 운영자는 Discord Developer Portal에서 자신의 Discord Application과 봇 토큰을 생성합니다. Installation은 **Guild Install만** 사용하고, 설치 scope는 `bot`, `applications.commands`로 설정합니다. 봇 권한은 다음만 부여합니다.

- `View Channel`
- `Send Messages`
- `Read Message History`
- `Embed Links`
- `Attach Files`

Bot 설정에서 privileged intent인 `Message Content`와 `Server Members`를 활성화합니다. 전자는 금지어 필터에, 후자는 퇴장 시 파티 정리에 필요합니다. 운영자 전용 앱은 `Public Bot`을 끄세요. 이는 Portal에서 하는 설정이며, 이 저장소의 코드가 Portal 설정을 변경하지는 않습니다.

슬래시 명령은 전역으로 등록되므로 초대 직후 반영까지 최대 1시간이 걸릴 수 있습니다. 봇을 서버에서 내보내면 해당 서버의 포인트·파티·설정은 자동으로 삭제됩니다.

### 2. 설치

```bash
touch .env.secrets .env.runtime
mkdir -p runtime/data runtime/backups runtime/logs
cp settings/forbidden_words.example.json settings/forbidden_words.json
chmod 600 .env.secrets .env.runtime settings/forbidden_words.json
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

### 3. 환경과 서버 설정

`.env.example`을 기준으로 다음 두 파일을 채웁니다.

- `.env.secrets`: 방금 만든 Discord 토큰과 OpenAI·Google 자격 증명
- `.env.runtime`: 데이터·백업 경로, 백업 주기, AI 쿨다운과 한도

채널 ID는 환경변수가 아닙니다. 봇을 Guild에 설치한 뒤 각 서버 관리자가 `/설정 모집채널`, `/설정 이벤트채널`로 지정하며, 값은 서버별 DB에 저장됩니다. 지정 전에는 해당 기능이 안내 메시지와 함께 잠깁니다.

실제 금지어 목록은 `settings/forbidden_words.json`에 작성합니다. 이 목록은 한 봇 인스턴스에 공통 적용되며, 경고 횟수는 서버별로 집계됩니다.

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

## 상세 운영

Docker Compose·launchd 실행, 백업, 복구, 배포, 롤백은 [운영 가이드](docs/operations.md)를 참고하세요.
