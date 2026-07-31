# Discord Bot HSR

여러 서버에서 쓸 수 있는 Discord 봇입니다. 출석·포인트, 파티 모집, 서버 이벤트, 금융 조회, 금지어 관리, GPT 대화와 이미지 생성을 제공합니다. Python 3.12 이상과 SQLite를 사용합니다.

각 서버의 데이터(포인트·파티·설정)는 서로 완전히 분리됩니다. 분리는 애플리케이션 코드가 아니라 스키마가 보장합니다 — 모든 테이블의 기본키에 `guild_id`가 포함됩니다.

## 주요 기능

- `/출석`, `/지갑`, `/프로필`, `/랭킹`
- `/모집`, `/파티`, `/나가기`, `/변경`
- 지정 채널의 `/이벤트`
- `/기본대화`, `/고급대화`, `/이미지`
- `/주가`와 금지어 경고
- `/설정` — 서버 관리자가 모집·이벤트 채널을 지정 (서버 관리 권한 필요)

## 빠른 시작

### 1. 설치

```bash
touch .env.secrets .env.runtime
mkdir -p runtime/data runtime/backups runtime/logs
cp settings/forbidden_words.example.json runtime/data/forbidden_words.json
chmod 600 .env.secrets .env.runtime runtime/data/forbidden_words.json
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

### 2. 환경 설정

`.env.example`을 기준으로 다음 두 파일을 채웁니다.

- `.env.secrets`: Discord, OpenAI, Google 자격 증명
- `.env.runtime`: 데이터·백업 경로, 백업 주기, AI 쿨다운

채널 ID는 환경변수가 아닙니다. 봇을 초대한 뒤 각 서버에서 `/설정 모집채널`,
`/설정 이벤트채널`로 지정하며, 값은 서버별로 DB에 저장됩니다. 지정 전에는 해당
기능이 안내 메시지와 함께 잠깁니다.

실제 금지어는 `runtime/data/forbidden_words.json`에 작성합니다. 이 목록은 봇
인스턴스 전체에 공통 적용되며, 경고 횟수는 서버별로 따로 집계됩니다.

### 3. DB 초기화와 초기 백업

새 설치에서만 빈 DB를 초기화합니다. 기존 운영 DB가 있다면 초기화하지 말고 [운영 가이드](docs/operations.md)의 복구 절차를 따르세요.

```bash
.venv/bin/python -c 'from module.database import create_attendance_repository, create_party_repository, create_guild_settings_repository; from module.backup import DATABASES, verify_database; from module.config import DATA_DIR; create_attendance_repository(); create_party_repository(); create_guild_settings_repository(); [print(name, verify_database(DATA_DIR / name, tables)) for name, tables in DATABASES.items()]'
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
```

### 4. 테스트와 실행

```bash
.venv/bin/python -m unittest test.test_discord_commands
.venv/bin/python -m test.console_tests
.venv/bin/python -m module.main
```

### 5. 봇 초대

Developer Portal에서 봇을 초대할 때 다음이 필요합니다.

- 권한: 메시지 보내기, 메시지 기록 보기, 슬래시 명령
- 인텐트: `MESSAGE CONTENT`, `SERVER MEMBERS` (금지어 필터와 퇴장 시 파티 정리에 사용)

슬래시 명령은 전역으로 등록되므로 초대 직후 반영까지 최대 1시간이 걸릴 수 있습니다.
봇을 서버에서 내보내면 그 서버의 포인트·파티·설정은 자동으로 삭제됩니다.

## 운영 시 주의

- `.env.secrets`, `.env.runtime`, `runtime/`은 Git에 추가하지 마세요. `git add -f`도 사용하지 않습니다.
- 운영 백엔드는 SQLite만 지원합니다.
- launchd와 Docker를 동시에 실행하지 마세요.
- 포인트 잔액은 서버별로 독립입니다. A 서버에서 번 포인트를 B 서버에서 쓸 수 없습니다.

## 상세 운영

launchd·Docker 실행, 백업, 복구, 배포, 롤백은 [운영 가이드](docs/operations.md)를 참고하세요.
