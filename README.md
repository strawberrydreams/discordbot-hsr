# 🤖 개인용 디스코드 봇 프로젝트 (Discord Bot HSR)

이미지

이 프로젝트는 **개인적인 디스코드 서버에서 사용하기 위해 제작된 커스텀 봇**입니다. 작성자의 개인적인 필요와 서버 환경에 맞춰 개발되었으며, 범용적인 사용보다는 특정 커뮤니티의 편의 기능을 제공하는 데 초점을 맞추고 있습니다.

## 📂 프로젝트 구조 및 모듈 설명

이 프로젝트의 핵심 기능은 `module/slash/` 디렉터리에 모듈화되어 있습니다.

### `module/slash/` (핵심 기능)

이 디렉터리의 파일들은 디스코드의 **Slash Command (슬래시 명령어)** 기능을 기반으로 동작하는 최신 코드들입니다.

| 파일명 | 설명 |
| :--- | :--- |
| **`main.py`** | 봇의 진입점(Entry Point)입니다. 봇을 실행하고, 다른 모듈(Cogs)들을 로드하며, 디스코드 서버와 연결하는 역할을 합니다. |
| **`config.py`** | 프로젝트의 환경 변수(API 키, 토큰, DB 백엔드 등)와 상수(채널 ID, 게임 설정 등)를 관리하는 설정 파일입니다. |
| **`database.py`** | **DB 접근 추상화 계층(Repository)** 입니다. 모든 SQL이 이 파일에 모여 있으며, 기본 SQLite 외에 MySQL, Oracle 등 외부 DB 구현을 추가해 교체할 수 있습니다. |
| **`attendance_cog.py`** | **출석체크 및 포인트 시스템**을 담당합니다. 매일 출석을 통해 포인트를 획득하고, 럭키박스(도박), 랭킹 확인, 프로필 조회 등의 기능을 제공합니다. (`attendance_data.db`) |
| **`playwith_cog.py`** | **게임 파티 모집 시스템**입니다. '모집', '참가', '나가기' 등의 명령어를 통해 게임별 파티를 구성하고 역할을 분배할 수 있습니다. (`party_data.db`) |
| **`hyacine_chat_cog.py`** | **OpenAI GPT 기반의 대화형 AI '히아킨'** 모듈입니다. 사용자와 자연스러운 대화를 나누며, 채널별로 '기본(GPT-5.4 mini)' 모드와 '고급(GPT-5.5 Thinking)' 모드를 전환할 수 있습니다. 고급 모드 사용 시 포인트를 소모합니다. |
| **`hyacine_image_cog.py`** | **Google Gemini (Nano Banana 2) 기반의 이미지 생성** 모듈입니다. 포인트를 소모하여 사용자가 요청한 그림을 그려줍니다. |
| **`eventnotice_cog.py`** | 디스코드 서버의 **예정된 이벤트 정보**를 조회하고 보여주는 알림 모듈입니다. |
| **`forbiddenfilter_cog.py`** | **강력한 금지어 필터링 시스템**입니다. 2중 필터(기본+변칙 표기 감지)를 통해 '아1니' 같은 회피 시도까지 잡아내며, 출석 모듈과 연동하여 경고 횟수를 기록합니다. |
| **`finance_cog.py`** | **실시간 금융 시세 조회** 모듈입니다. `/주가` 명령어로 주요 지표(주식, 코인, 유가 등)를 확인합니다. |

---

### 🏚️ Legacy Code (`single/`, `module/prefix/`)

*   **`single/`**: 단순한 임베드 공지 메시지를 전송할 수 있는 일회용 스크립트(`command_prefix.py`) 하나만 템플릿으로 보존되어 있습니다.
*   **`module/prefix/`**: Slash Command가 도입되기 전, `!`와 같은 접두사(Prefix)를 사용하여 명령어를 처리하던 구버전 모듈들입니다. 현재 프로젝트의 주력은 `module/slash/`의 슬래시 명령어입니다.

---

## 🛠️ 사용 및 커스텀 가이드

이 봇을 당신의 개인 서버에 맞게 커스텀하여 사용하려면 다음 단계가 필요합니다.

### 1. 필수 설정 (`config.py` 및 환경 변수)

`settings/` 디렉터리에 다음 `.env` 파일들을 생성하고 키를 입력해야 합니다.

*   `DISCORD_TOKEN.env`: `DISCORD_TOKEN=your_token_here`
*   `OPENAI_API_KEY.env`: `OPENAI_API_KEY=sk-...`
*   `GOOGLE_API_KEY.env`: `GOOGLE_API_KEY=AIza...`

또한 `module/slash/config.py` 파일에서 다음 ID들을 본인의 서버에 맞게 수정해야 합니다.

```python
# module/slash/config.py

RECRUIT_CHANNEL_ID = 1234567890... # 파티 모집을 진행할 채널 ID
EVENT_CHANNEL_ID = 1234567890...   # 이벤트 알림을 띄울 채널 ID
```

### 2. 주요 명령어 (Slash Commands)

봇이 실행되면 디스코드 입력창에서 `/`를 눌러 다음 명령어들을 사용할 수 있습니다.

#### 📝 출석 & 포인트
*   `/출석`: 매일 포인트를 획득합니다.
*   `/지갑`: 내 포인트 잔액을 확인합니다.
*   `/럭키박스 [금액]`: 포인트를 걸고 도박을 합니다.
*   `/랭킹`: 포인트 부자 순위를 봅니다.
*   `/프로필 [유저]`: 유저의 상세 정보를 봅니다.

#### 🎮 파티 모집
*   `/모집`: (모집 채널 전용) 게임 파티 모집창을 띄웁니다.
*   `/참가`, `/나가기`: 파티에 참여하거나 나갑니다.
*   `/파티`: 현재 모집 중인 파티 현황을 봅니다.

#### 🤖 AI (히아킨)
*   `/대화 [내용]`: 히아킨과 대화합니다.
*   `/이미지 [프롬프트]`: 그림을 그려달라고 요청합니다 (포인트 소모).
*   `/고급`, `/기본`: GPT 모델을 전환합니다.

#### 🛡️ 기타
*   `/이벤트 [번호]`: 서버 이벤트를 확인합니다.

#### 📈 금융
*   `/주가`: 주요 금융 지표(S&P500, 나스닥, 국채, 유가, 비트코인 등)의 실시간 시세를 조회합니다.

---

## ☁️ 클라우드 배포 가이드

이 봇은 Python 앱을 실행할 수 있는 어떤 클라우드 서비스(PaaS, VPS, 컨테이너 등)에도 배포할 수 있습니다. 서비스별 메뉴 이름은 다르지만, 공통적으로 아래 네 가지만 설정하면 됩니다.

### 1. 실행 환경
*   **Python 버전**: `3.11` 이상 권장
*   **의존성 설치 (빌드 명령어)**: `pip install -r requirements.txt`
*   **시작 명령어**: `python -m module.slash.main` (프로젝트 루트 기준)
*   저장소를 사용할 경우 **비공개(Private)** 로 유지하세요.

### 2. 환경 변수 (Environment Variables)
배포 플랫폼의 환경 변수(또는 Secret) 설정에 다음을 등록합니다. 비밀 값은 반드시 Secret으로 표시하세요.

| 변수 | 필수 | 설명 |
| :--- | :--- | :--- |
| `DISCORD_TOKEN` | ✅ | 디스코드 봇 토큰 |
| `OPENAI_API_KEY` | ✅ | OpenAI API 키 (대화 기능) |
| `GOOGLE_API_KEY` | ✅ | Google Gemini API 키 (이미지 생성) |
| `DATA_DIR` | | SQLite DB 파일 저장 경로 (기본값: `data`) |
| `DB_BACKEND` | | DB 백엔드 선택 (기본값: `sqlite`) |
| `DB_URL` | | 외부 DB 접속 문자열 (외부 DB 사용 시) |

### 3. 데이터 영속성 (중요!)
기본 SQLite 사용 시, 컨테이너 기반 플랫폼은 재배포·재시작 때 파일 시스템이 초기화되어 **DB 데이터가 소실됩니다**. 둘 중 하나를 선택하세요.

*   **영구 볼륨(Persistent Volume) 마운트**: 플랫폼의 볼륨/파일 시스템 기능으로 디스크를 마운트하고, 마운트 경로를 `DATA_DIR` 환경 변수와 일치시킵니다. (예: `/app/data`)
*   **외부 DB 사용**: MySQL, Oracle 등 관리형 DB를 쓰면 볼륨 설정이 필요 없습니다. 아래 '외부 DB 연결' 참고.

### 4. 외부 DB 연결 (선택)
DB 접근은 `module/slash/database.py`의 Repository 계층으로 추상화되어 있어, Cog 코드를 건드리지 않고 백엔드를 교체할 수 있습니다.

1.  `database.py`에 `AttendanceRepository` / `PartyRepository`를 상속한 구현 클래스를 작성합니다. (SQL placeholder 등 방언 차이만 처리하면 됩니다)
2.  같은 파일의 `create_attendance_repository` / `create_party_repository` 팩토리에 분기를 추가합니다.
3.  환경 변수를 설정합니다: `DB_BACKEND=mysql`, `DB_URL=mysql://user:pass@host:3306/botdb`

---

## 🧪 테스트

디스코드 연결 없이 콘솔에서 DB 계층과 핵심 로직을 검증할 수 있습니다.

```bash
python -m test.console_tests
```

테스트는 임시 디렉터리의 격리된 DB를 사용하므로 운영 데이터를 건드리지 않습니다.

---

## ⚠️ 주의사항

*   이 봇은 기본적으로 `attendance_data.db`와 `party_data.db`라는 SQLite DB 파일을 `DATA_DIR`(기본값 `data/`)에 생성하여 데이터를 저장합니다. 봇을 재설치하거나 이동할 때 이 파일들을 백업해야 데이터가 유지됩니다.
*   `forbidden_words.json` 파일이 `settings/` 디렉터리에 있어야 금지어 필터가 정상 작동합니다.
