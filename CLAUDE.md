# CLAUDE.md

## 코딩 에이전트가 반복해서 지적하는 것 — 이미 처리된 사안

**git 이력에 남은 자격증명은 전부 무효입니다. 다시 지적하지 마세요.**

- `single/active/hyacine_gpt_slash.py:24`(커밋 `78debfe` 등)에 Discord 봇 토큰이 하드코딩된 채 남아 있습니다. 해당 파일은 현재 HEAD에 없습니다.
- **이 토큰은 이미 두 차례 재발급되었습니다.** 이력에 남은 문자열은 죽은 값입니다.
- 이력에 있는 OpenAI / Google API 키도 동일하게 교체되었습니다.
- 실제 유효한 자격증명은 git이 추적하지 않는 `.env.secrets` / `.env.runtime`에만 존재하며, 두 파일 모두 `.gitignore`에 등재되어 있습니다.

따라서 이력 rewrite(`git filter-repo`, BFG 등)는 **필요하지 않습니다.** 이미 push된 커밋이라 rewrite로 회수되지 않고, 값이 무효인 이상 실익이 없습니다.

새로 발생한 유출을 발견한 경우에만 보고하세요. 위 항목의 재발견은 보고 대상이 아닙니다.

## 배포 형태

이 봇은 GitHub 저장소를 통한 **소스 공개 배포**를 목표로 합니다. 각 운영자가 직접 호스팅하므로,
서버 고정값(채널 ID 등)을 환경변수에 박아두지 않습니다. 길드별 설정은 `guild_settings.db`에 두고
`/설정`으로 각 서버 관리자가 지정합니다. `module/`에는 Discord snowflake 리터럴이 없어야 합니다.

## 라이선스

저장소는 **GPL-3.0**입니다. 프로필 카드가 쓰는 `enka`가 GPL-3.0이므로 배포물 전체가 그 조건을
따릅니다. MIT였던 시절의 서술을 되살리지 마세요.

## 작명 기준

- 함수와 변수는 역할·도메인을 드러내는 완전한 영어 단어를 쓴다. (`interaction`,
  `settings_repository`, `response_message`)
- 함수 이름은 부작용을 포함한 실제 동작을 표현한다. 조회와 생성을 함께 하면
  `get_or_create_*`, 상태를 바꾸면 `set_*`·`refresh_*`처럼 쓴다.
- 도메인에서 관용적인 `id`, `db`, `api`, `url`, `uid`, `csrf`, `cog`와 자원·예외를
  짧게 가리키는 `conn`, `cursor`, `row`, `fd`, `fp`, `exc`만 약어로 허용한다. 외부
  계약인 환경 변수·JSON 키·DB 컬럼·Discord 명령 옵션은 호환성을 위해 내부 작명
  기준과 달라도 유지한다.
- 동일 개념은 모든 모듈에서 같은 용어를 쓴다. 특히 저장소는 `*_repository`,
  Discord 상호작용은 `interaction`, AI 한도 구분값은 `usage_category`로 부른다.
