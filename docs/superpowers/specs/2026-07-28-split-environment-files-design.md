# Split Environment Files Design

## Goal

민감정보와 일반 런타임 설정을 서로 다른 로컬 파일에 보관한다. 공개
GitHub 저장소에는 전체 환경변수 형식을 보여 주는 `.env.example`만
추적하고, 실제 값이 들어 있는 파일은 Git과 Docker 이미지에서 제외한다.

## File Contract

### `.env.secrets`

노출되면 자격 증명이 손상되는 값만 저장한다.

```dotenv
DISCORD_TOKEN=
OPENAI_API_KEY=
GOOGLE_API_KEY=
```

### `.env.runtime`

노출되더라도 자격 증명 자체가 손상되지는 않는 운영 설정을 저장한다.

```dotenv
RECRUIT_CHANNEL_ID=
EVENT_CHANNEL_ID=
DATA_DIR=runtime/data
BACKUP_DIR=runtime/backups
FORBIDDEN_WORDS_FILE=settings/forbidden_words.json
BACKUP_INTERVAL_SECONDS=21600
BACKUP_RETENTION_DAYS=30
DB_BACKEND=sqlite
```

### `.env.example`

Git에 계속 추적하는 유일한 공개 예제 파일이다. 위 두 파일의 모든 변수와
어느 파일에 넣어야 하는지를 주석으로 보여 주는 변수 카탈로그 역할을 한다.
실제 값은 포함하지 않는다.

별도의 `.env.secrets.example`과 `.env.runtime.example`은 만들지 않는다.
동일한 변수 목록을 여러 파일에서 관리하지 않기 위함이다.

## Loading and Precedence

`module/config.py`는 프로젝트 루트를 기준으로 다음 순서로 파일을 로드한다.

1. `.env.secrets`
2. `.env.runtime`

두 호출 모두 `python-dotenv`의 기본 `override=False`를 사용한다. 따라서
프로세스 환경변수가 가장 우선하며, 파일에 중복 키가 있다면 먼저 로드된
`.env.secrets`의 값이 유지된다. 정상 구성에서는 두 파일에 중복 키를 두지
않는다.

기존의 필수값·양의 정수·SQLite 전용 검증은 그대로 유지한다. 파일이
없거나 필수값이 비어 있으면 기존 `validate_config()`가 시작을 실패시킨다.

## Privacy Boundaries

- `.env.secrets`, `.env.runtime`, 기존 `.env`는 Git에서 제외한다.
- 세 실제 파일은 Docker build context와 이미지에서도 제외한다.
- `.env.example`은 Git과 Docker ignore 규칙에 포함하지 않는다.
- Docker Compose의 `bot`과 `backup` 서비스는 `.env.secrets`와
  `.env.runtime`을 모두 `env_file`로 받는다.
- 두 실제 파일은 로컬에서 권한 `0600`으로 유지한다.

## Migration

현재 ignore된 `.env`를 값 출력 없이 파싱해 다음과 같이 이전한다.

- 세 자격 증명은 `.env.secrets`로 이동한다.
- 나머지 지원 설정은 `.env.runtime`으로 이동한다.
- 두 결과 파일의 필수 키 존재 여부와 권한을 메타데이터로 검증한다.
- 이전이 성공한 뒤 기존 `.env`를 삭제한다.

현재 채널 ID 자리표시자 `1/1`은 `.env.runtime`으로 이동하지만 실제
배포를 시작할 수 있는 값으로 간주하지 않는다.

## Documentation

README의 초기 설치, 개인정보 경계, Docker, 백업 설정 설명을 두 파일
기준으로 바꾼다. `.env.example`은 참고용 카탈로그이며 그대로 복사해
운영 파일로 쓰지 않도록 명시한다.

실제 봇 시작 전 `RECRUIT_CHANNEL_ID`와 `EVENT_CHANNEL_ID`를 유효한 Discord
채널 ID로 교체해야 한다는 차단 조건을 유지한다.

## Verification

- 설정 모듈이 현재 작업 디렉터리와 무관하게 두 파일을 로드한다.
- 프로세스 환경변수가 두 파일보다 우선한다.
- 공개 예제 변수 집합은 실제 두 파일의 계약과 일치하고 중복이 없다.
- 전체 콘솔 테스트가 통과한다.
- Compose가 두 env 파일을 두 서비스에 모두 적용한다.
- Git은 실제 env 파일을 추적하지 않고 `.env.example`은 계속 추적한다.
- Docker 이미지에는 실제 env 파일 세 종류가 모두 없다.
- 기존 `.env` 삭제 후에도 설정 검증과 Docker build가 통과한다.

## Out of Scope

- 외부 secret manager
- 자격 증명 자동 회전
- 실제 Discord 채널 ID 선택
- 과거 Git 이력 재작성
