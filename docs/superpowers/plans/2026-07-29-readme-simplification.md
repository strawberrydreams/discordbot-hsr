# README Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** README를 빠른 시작 문서로 줄이고 상세 운영 절차를 `docs/operations.md`로 보존한다.

**Architecture:** `README.md`는 프로젝트 소개, 설치, 설정, 초기화, 테스트, 로컬 실행, 핵심 경고와 운영 문서 링크만 담당한다. `docs/operations.md`는 기존 README의 launchd/Docker, 백업, 복구, 배포, 롤백, 호스트 한계 절차를 그대로 담당한다.

**Tech Stack:** Markdown, Python 3.12+, Git

## Global Constraints

- `README.md`와 새 `docs/operations.md`만 구현 대상으로 변경한다.
- 기존 운영 명령과 안전 경고를 삭제하지 않고 운영 문서로 이동한다.
- 같은 상세 절차를 두 문서에 중복하지 않는다.
- 환경변수 이름은 `.env.example`을 단일 카탈로그로 사용한다.
- 코드, 환경변수 계약, 배포 파일, 백업 동작은 변경하지 않는다.

---

### Task 1: README와 운영 런북 분리

**Files:**
- Modify: `README.md`
- Create: `docs/operations.md`

**Interfaces:**
- Consumes: 기존 README의 `실행 방식 선택`부터 `호스트 한계`까지의 운영 절차
- Produces: 빠른 시작용 `README.md`, 상세 운영용 `docs/operations.md`

- [ ] **Step 1: 상세 운영 문서 작성**

`docs/operations.md`를 다음 구조로 만들고 기존 README의 관련 설명과 명령 블록을 빠짐없이 이동한다.

```markdown
# Discord Bot HSR 운영 가이드

최초 설치와 환경 설정은 [README](../README.md)를 먼저 따르세요.

> launchd와 Docker를 동시에 실행하지 마세요. 같은 Discord 토큰과 SQLite 파일을 두 프로세스가 함께 사용하게 됩니다.

## 실행 방식 선택
### macOS LaunchAgent
### Docker Desktop
## 백업 운영
## 검증된 백업으로 실제 복구
## 배포
## 코드 롤백
## 호스트 한계
```

- [ ] **Step 2: README를 빠른 시작 문서로 교체**

README heading 구조는 다음으로 제한한다.

```markdown
# Discord Bot HSR
## 주요 기능
## 빠른 시작
### 1. 설치
### 2. 환경 설정
### 3. DB 초기화와 초기 백업
### 4. 테스트와 실행
## 운영 시 주의
## 상세 운영
```

빠른 시작에는 다음 실제 명령을 포함한다.

```bash
touch .env.secrets .env.runtime
chmod 600 .env.secrets .env.runtime
cp settings/forbidden_words.example.json settings/forbidden_words.json
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
mkdir -p runtime/data runtime/backups runtime/logs
.venv/bin/python -c 'from module.database import create_attendance_repository, create_party_repository; from module.backup import DATABASES, verify_database; from module.config import DATA_DIR; create_attendance_repository(); create_party_repository(); [print(name, verify_database(DATA_DIR / name, tables)) for name, tables in DATABASES.items()]'
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup verify
.venv/bin/python -m module.backup restore-test
.venv/bin/python -m unittest test.test_discord_commands
.venv/bin/python -m test.console_tests
.venv/bin/python -m module.main
```

운영 시 주의에는 비밀 파일 강제 추가 금지, SQLite 전용, launchd/Docker 동시 실행 금지를 남긴다. 상세 운영에는 `[운영 가이드](docs/operations.md)` 링크만 둔다.

- [ ] **Step 3: 문서 구조와 보존 검증**

Run:

```bash
test -f docs/operations.md
rg -n '^## (실행 방식 선택|백업 운영|검증된 백업으로 실제 복구|배포|코드 롤백|호스트 한계)$' docs/operations.md
rg -n 'docs/operations.md|git add -f|launchd와 Docker' README.md
test "$(wc -l < README.md)" -lt 120
git diff --check
```

Expected: 운영 heading 6개와 README의 링크·안전 경고가 모두 검색되고, README는 120줄 미만이며 모든 명령이 exit 0.

- [ ] **Step 4: 기존 전체 검증**

Run:

```bash
/Users/strawberrydreams/coding/discordbot-hsr/.venv/bin/python -m unittest test.test_discord_commands
/Users/strawberrydreams/coding/discordbot-hsr/.venv/bin/python -m test.console_tests
```

Expected: 명령 테스트 12개 PASS, 콘솔 검사 109개 PASS.

- [ ] **Step 5: 커밋**

```bash
git add README.md docs/operations.md
git commit -m "docs: simplify README and split operations guide"
```
