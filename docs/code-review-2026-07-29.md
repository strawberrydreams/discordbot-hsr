# 코드 리뷰 — 2026-07-29

대상: `module/` 9개 파일(~1,800줄), 배포 설정(`Dockerfile`, `compose.yaml`, `deploy/macos/*.plist.example`), 테스트.
관점: 기능 / 동시성 / 충돌 / 배포.

테스트는 리뷰 시점에 전부 통과합니다 — `unittest` 12개 + `console_tests` 109개 = 121개.

---

## 1) 기능

### 🔴 `@everyone` 에코 — `module/hyacine_chat_cog.py:238`

`await inter.followup.send(f"**{mention}**: {내용}")` 가 사용자 입력을 일반 메시지로 그대로 되돌립니다. `MyBot`(`module/main.py:26`)에 `allowed_mentions`가 없어 discord.py 기본값(`AllowedMentions.all()`)이 적용됩니다. `/기본대화 내용:@everyone` → 봇이 서버 전체를 멘션합니다.

→ 근본 수정은 한 줄, 봇 생성자에서: `allowed_mentions=discord.AllowedMentions.none()`. 모든 Cog가 한 번에 커버됩니다.

### 🔴 유령 파티 — `module/playwith_cog.py:124-127`, `233-254`

`/파티`(조회 명령)가 참가자 0명인 파티를 **삭제**합니다. 그런데 파티 생성 직후(`GameSelect.callback` → `create_party` → 모집 메시지 게시)는 항상 참가자 0명입니다. 그 사이 누가 `/파티`를 치면 파티 행이 사라지고, 모집 메시지의 영속 버튼(`custom_id=party:join:{game}`)은 그대로 살아있습니다. 버튼 콜백은 `get_party()`를 확인하지 않으므로 → 존재하지 않는 파티에 participant 행이 생기고, 그 유저는 `/파티`에 안 보이면서 "이미 다른 파티에 참가 중"으로 다른 파티에 못 들어갑니다. `cleanup_parties`는 `parties` 행 기준이라 이 고아 행을 영원히 못 지웁니다(`/나가기`로만 탈출).

→ `JoinButton.callback` 맨 앞에 `if not self.cog.get_party(game): return "모집이 종료된 파티입니다"` 추가 + 조회 명령인 `/파티`에서 삭제 로직 제거.

### 🟠 이중 환불 — `module/hyacine_image_cog.py:77` + `106`

`image_data is None` 분기에서 환불한 뒤 `await inter.followup.send(...)`가 실패하면 바깥 `except`가 **또** 환불합니다(50,000 → 100,000 P). 성공 경로(`:99`)에서 업로드만 실패해도 전액 환불됩니다(이미지는 이미 생성되어 Google 요금은 나간 상태).

→ `hyacine_chat_cog.py:183-194`의 `charged`/`refunded` 플래그 패턴이 이미 저장소에 있습니다. 그대로 가져다 쓰세요.

### 🟡 로그 유실 — `module/forbiddenfilter_cog.py:107`

여기만 `logging.info`를 쓰는데 root logger를 아무도 설정하지 않습니다(discord.py는 자기 로거만 설정). "📥 금지어 N개 로드"는 `bot.log`에 절대 안 찍힙니다. 나머지 파일은 전부 `print`.

### 🟡 `/이벤트`

번호로만 조회 가능한데 목록 명령이 없고, `fetch_scheduled_events()`의 순서는 보장되지 않아 번호가 흔들립니다.

### ⚪ 미사용 코드

`single/command_prefix.py`, `test/hello_prefix.py`, `test/makejson.py`는 어디에도 연결되어 있지 않습니다.

---

## 2) 동시성

### 🔴 이벤트 루프 블로킹 — `module/finance_cog.py:65`

`yf.Ticker().fast_info`는 동기 HTTP입니다. `/주가` 하나가 티커 6개를 **순차 동기 호출**로 돌리는 동안 봇 전체(다른 명령, gateway heartbeat)가 멈춥니다. 네트워크가 느리면 heartbeat blocked → 연결 재수립.

→ 올바른 패턴이 이미 `hyacine_image_cog.py:60`(`run_in_executor`)에 있습니다. `asyncio.to_thread` + `asyncio.gather`로 6개 병렬 처리하면 코드도 줄고 응답도 빨라집니다.

### 🟢 포인트 원자성은 잘 되어 있음

`deduct_points`의 조건부 UPDATE(`database.py:162`), `play_luckybox`의 `BEGIN IMMEDIATE`(`:218`) 둘 다 정확하고, `console_tests`가 스레드 경합으로 실제 검증까지 합니다. 이 부분은 손댈 필요 없습니다.

### 🟠 `/출석`만 read-modify-write — `attendance_cog.py:46` → `:61`

`update_attendance`는 절대값을 씁니다. 지금 안전한 건 **읽기와 쓰기 사이에 `await`가 없어서**일 뿐입니다(단일 스레드 루프). 나중에 누가 그 사이에 `await`를 하나 넣거나, 프로세스가 두 개가 되면 즉시 lost update가 되어 럭키박스/환불 결과를 덮어씁니다.

→ 다른 메서드처럼 조건부 UPDATE 한 방으로 (`SET points = points + ?, last_attendance_date = ? WHERE user_id = ? AND (last_attendance_date IS NULL OR last_attendance_date != ?)`) 바꾸면 중복 출석 방지까지 DB가 해줍니다.

### 🟡 세션 누수 — `hyacine_chat_cog.py:69`

`sessions`는 채널마다 생기고 절대 안 지워집니다. 그리고 히스토리에 Discord CDN 이미지 URL(서명 만료 ~24h)이 남아 나중 턴에 만료 URL을 OpenAI로 재전송합니다.

### 🟡 시간대 — `playwith_cog.py:30`, `:41`

naive `datetime.now()`. 서버 TZ가 바뀌면 24시간 만료가 어긋나고, Python 3.12는 암묵적 datetime↔SQLite 어댑터를 deprecate 했습니다.

### 🟡 SQLite 튜닝

호출마다 새 커넥션, journal_mode 기본값, timeout 5초 기본. 현재 규모에선 문제없지만 `database is locked`가 보이기 시작하면 WAL + `timeout` 인자가 조절 손잡이입니다.

---

## 3) 충돌

### 🔴 이중 인스턴스가 문서로만 막혀 있음

`README.md`와 `docs/operations.md:5`가 "launchd와 Docker를 동시에 실행하지 마세요"라고 경고하지만 **코드가 강제하지 않습니다**. 같은 토큰 + 같은 SQLite 파일 두 프로세스 = 모든 명령 중복 응답, `/출석` lost update, `database is locked`. 사람이 실수하면 조용히 데이터가 깨집니다.

→ `backup.py:127`이 이미 `.backup.lock` + `fcntl.flock`으로 정확히 이 문제를 풀었습니다. `main()`에 같은 패턴 5줄이면 두 번째 인스턴스가 시작 즉시 죽습니다.

### 🟠 백업 스케줄이 두 곳에 있음

plist의 `StartInterval`(21600)과 `.env.runtime`의 `BACKUP_INTERVAL_SECONDS`(21600)가 별개로 관리됩니다. flock 덕에 동시 실행은 안전하지만 값이 반드시 어긋납니다. `docs/operations.md:62`도 "둘 다 고쳐야 한다"고 적고 있는데, 그게 바로 설계 냄새입니다.

### 🟠 매 부팅 글로벌 sync — `main.py:42`

`tree.sync()`는 글로벌 동기화입니다. `KeepAlive=true`(plist) / `restart: unless-stopped`(compose)와 크래시 루프가 만나면 반복 호출됩니다. 개인 서버 하나면 길드 sync(`copy_global_to(guild)` + `sync(guild=...)`)가 즉시 반영되고 rate limit도 안전합니다.

### 🟡 이미지 2회 빌드

`compose.yaml`의 `bot`과 `backup`이 각각 `build: .`. 동일 이미지를 두 번 빌드합니다.

### 🟢 명령어 이름 충돌은 없음

7개 Cog의 slash command 이름을 전부 대조했고 중복 없습니다.

---

## 4) 배포

### 🟠 신규 설치 크래시 — `main.py:33-34`

Cog가 DB를 만들기 **전에** `verify_database`가 돌아서, DB 파일이 없으면 `RuntimeError: DB 파일이 없습니다`로 죽습니다. README 3단계의 초기화 one-liner가 이걸 전제로 하는데, **Docker 경로에는 대응하는 초기화 절차가 `operations.md`에 없습니다**. 깨끗한 호스트에서 `docker compose up -d` → 크래시 루프.

→ 사전 검사는 "파일이 있을 때만 검증"으로 완화하고, 로드 후 검증(`:40-41`)은 그대로 두면 의도(손상 DB로 시작 거부)는 유지됩니다.

### 🟠 forbidden_words.json 바인드 마운트 함정 — `compose.yaml`

호스트에 파일이 없으면 Docker가 같은 이름의 **디렉터리**를 만들어버립니다. README 1단계가 `cp`를 시키긴 하지만, 빠뜨리면 원인 찾기 어려운 실패가 됩니다.

### 🟠 로그 로테이션 없음

launchd는 `runtime/logs/*.log`에 무한 append(newsyslog 설정 없음), compose에는 `logging:` 드라이버 제한이 없습니다. 오래 돌리면 디스크가 찹니다.

### 🟡 실패가 조용함

`restart: unless-stopped` / `KeepAlive`가 영구 실패(토큰 폐기 등)를 감춥니다. healthcheck도 알림도 없어서 봇이 죽은 걸 사람이 눈치채야 합니다.

### 🟢 시크릿 위생은 통과

`.gitignore`/`.dockerignore` 양쪽 다 두 env 파일 커버, 퍼미션 600, 이미지에는 `module/`만 들어감. **git 이력 전체를 스캔했는데 실제 키 패턴은 없습니다** — 과거 커밋 `78debfe`의 `settings/NULL_API_KEY.env`는 내용이 `"이곳에 필요한 API 키 값을 추가하세요."` 플레이스홀더였습니다. (기존에 남아 있던 "git 이력 키 유출" 우려는 실제 유출이 아니었습니다.) 다만 같은 커밋에 실제 `settings/forbidden_words.json`이 들어가 있으니, 그 목록이 민감하면 이력 정리 대상입니다.

### 🟢 백업 설계는 이 저장소에서 제일 잘 된 부분

파일+디렉터리 fsync, temp + `os.replace` 원자적 공개, sha256 manifest, flock, restore-test, 경로 가드가 있는 보존 정책까지. 손대지 마세요.

### 🟡 진짜 가용성 병목은 코드가 아니라 호스트

`operations.md:263-268`에 이미 적힌 대로 Mac 절전이면 launchd도 Docker도 멈춥니다. 업타임이 중요해지는 순간 코드 튜닝이 아니라 호스트를 옮겨야 합니다.

