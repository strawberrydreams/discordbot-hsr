# Discord Bot HSR

개인 Discord 서버용 봇입니다. 출석·포인트, 파티 모집, 서버 이벤트, 금융 조회, 금지어 관리, GPT 대화와 이미지 생성을 제공합니다. Python 3.12 이상과 SQLite를 사용합니다.

## 주요 기능

- `/출석`, `/지갑`, `/프로필`, `/럭키박스`, `/랭킹`
- `/모집`, `/파티`, `/나가기`, `/변경`
- 지정 채널의 `/이벤트`
- `/기본대화`, `/고급대화`, `/이미지`
- `/주가`와 금지어 경고

## 빠른 시작

### 1. 설치

```bash
touch .env.secrets .env.runtime
chmod 600 .env.secrets .env.runtime
cp settings/forbidden_words.example.json settings/forbidden_words.json
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
mkdir -p runtime/data runtime/backups runtime/logs
```

### 2. 환경 설정

`.env.example`을 기준으로 다음 두 파일을 채웁니다.

- `.env.secrets`: Discord, OpenAI, Google 자격 증명
- `.env.runtime`: 채널 ID, 데이터·백업 경로, 백업 설정

실제 금지어는 `settings/forbidden_words.json`에 작성합니다.

### 3. DB 초기화와 초기 백업

새 설치에서만 빈 DB를 초기화합니다. 기존 운영 DB가 있다면 초기화하지 말고 [운영 가이드](docs/operations.md)의 복구 절차를 따르세요.

```bash
.venv/bin/python -c 'from module.database import create_attendance_repository, create_party_repository; from module.backup import DATABASES, verify_database; from module.config import DATA_DIR; create_attendance_repository(); create_party_repository(); [print(name, verify_database(DATA_DIR / name, tables)) for name, tables in DATABASES.items()]'
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

## 운영 시 주의

- `.env.secrets`, `.env.runtime`, `settings/forbidden_words.json`, `runtime/`은 Git에 추가하지 마세요. `git add -f`도 사용하지 않습니다.
- 운영 백엔드는 SQLite만 지원합니다.
- launchd와 Docker를 동시에 실행하지 마세요.

## 상세 운영

launchd·Docker 실행, 백업, 복구, 배포, 롤백은 [운영 가이드](docs/operations.md)를 참고하세요.
