"""웹 관리 화면의 문자열 카탈로그와 언어 결정.

화면 문구는 템플릿의 ``{{t:key}}``로, 서버가 만드는 문구는 ``translate()``로
꺼낸다. 두 경우 모두 web_admin_cog의 기존 escape 규칙을 그대로 탄다.

언어를 결정하는 순서는 ``?lang=`` 쿼리 → ``admin_lang`` 쿠키 → ``Accept-Language``
헤더 → 기본 ``ko``다. 전환은 평범한 링크(``?lang=en``)라 JavaScript가 필요 없고
CSP를 건드리지 않는다.

**두 언어의 키 집합은 반드시 같아야 한다.** 키가 빠지면 화면에 조용히 빈
문자열이 렌더되므로, 테스트가 이 동등성을 검사한다.
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "ko"
SUPPORTED_LANGUAGES = ("ko", "en")
LANGUAGE_COOKIE = "admin_lang"
LANGUAGE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60

STRINGS: dict[str, dict[str, str]] = {
    "ko": {
        # ── 화면 공통 ──
        "html_lang": "ko",
        "login_title": "{bot_name} 관리자 로그인",
        "index_title": "{bot_name} Bot Control Center",
        "brand_meta": "Bot admin",
        "skip_to_content": "본문으로 건너뛰기",
        "localhost_protected": "127.0.0.1:8080 · Localhost protected",
        "language_switch_label": "언어",
        "language_korean": "한국어",
        "language_english": "English",
        # ── 로그인 화면 ──
        "login_hero_heading": "Bot Control Center",
        "login_hero_copy": "페르소나와 콘텐츠 설정, 길드 운영 현황을 로컬 환경에서 안전하게 관리하세요.",
        "login_eyebrow": "Administrator access",
        "login_heading": "관리자 로그인",
        "login_copy": "환경변수에 설정한 관리 토큰을 입력하세요.",
        "login_token_label": "관리 토큰",
        "login_token_helper": "토큰은 이 로그인 요청 외에는 화면에 표시되지 않습니다.",
        "login_submit": "로그인",
        # ── 사이드바·상단 바 ──
        "sidebar_label": "관리 메뉴",
        "nav_section_label": "관리 화면 바로가기",
        "nav_label": "Control center",
        "nav_overview": "개요",
        "nav_persona": "페르소나",
        "nav_content": "콘텐츠 설정",
        "nav_guilds": "길드 현황",
        "nav_announcement": "공지 보내기",
        "boundary_title": "Localhost protected",
        "boundary_copy": "이 관리 화면은 127.0.0.1:8080에서만 접근할 수 있습니다.",
        "breadcrumb_console": "관리자 콘솔",
        "session_extend": "세션 연장",
        "logout": "로그아웃",
        # ── 개요 ──
        "page_eyebrow": "Local administration",
        "page_title": "Bot Control Center",
        "page_copy": "페르소나와 콘텐츠 설정을 관리하고, 참여 중인 Discord 길드의 운영 상태를 확인하세요.",
        "status_chip": "ADMIN ONLINE",
        "overview_label": "관리 서버 운영 요약",
        "metric_connection": "접속 상태",
        "metric_connection_value": "정상",
        "metric_scope": "관리 범위",
        "metric_scope_value": "Local",
        "metric_settings_files": "설정 파일",
        # ── 봇 설정 ──
        "settings_title": "봇 설정",
        "settings_copy": "운영 중 자주 확인하는 설정을 한곳에서 관리합니다.",
        "persona_kicker": "AI identity",
        "persona_title": "페르소나",
        "persona_apply": "새 AI 세션부터 적용",
        "persona_prompt_label": "System prompt",
        "persona_prompt_helper": "최대 16,000자",
        "persona_greeting_label": "인삿말",
        "persona_greeting_helper": "새 대화가 시작될 때 사용됩니다.",
        "persona_submit": "페르소나 저장",
        "required": "필수",
        "forbidden_kicker": "Moderation",
        "forbidden_title": "금지어",
        "forbidden_apply": "즉시 적용",
        "forbidden_submit": "저장 및 다시 불러오기",
        "games_kicker": "Party finder",
        "games_title": "게임 목록",
        "games_apply": "재시작 후 적용",
        "games_submit": "게임 목록 저장",
        # ── 길드 ──
        "guilds_title": "길드별 설정",
        "guilds_copy": "길드별 파티·공지·이벤트 채널과 웹 공지·금지어 필터 사용 여부를 저장합니다.",
        "guilds_table_label": "길드별 채널·공지·금지어 설정",
        "guilds_column_guild": "길드",
        "guilds_column_settings": "채널 및 기능 설정",
        # ── 공지 ──
        "announce_title": "옵트인 공지",
        "announce_copy": "공지를 허용한 길드의 지정 채널로 Embed 메시지를 보냅니다.",
        "announce_target": "대상 길드",
        "announce_color": "Embed 색상",
        "announce_title_label": "제목",
        "announce_body_label": "본문",
        "announce_syntax_summary": "Discord 문법 가이드",
        "announce_syntax_bold": "굵게",
        "announce_syntax_italic": "기울임",
        "announce_syntax_underline": "밑줄",
        "announce_syntax_strike": "취소선",
        "announce_syntax_inline": "인라인 코드",
        "announce_syntax_quote": "인용",
        "announce_syntax_list": "목록",
        "announce_syntax_link": "링크 텍스트",
        "announce_syntax_mention": "멘션",
        "announce_syntax_helper": "입력한 줄바꿈과 Discord Markdown이 Embed 본문에 그대로 적용됩니다.",
        "announce_image_label": "이미지",
        "announce_image_helper": "선택 사항 · PNG, JPEG, GIF, WebP · 최대 8 MiB",
        "announce_submit": "공지 보내기",
        "announce_note_label": "공지 전송 안내",
        "announce_note_title": "전송 전 확인",
        "announce_note_1": "길드가 호스트 공지를 허용했는지 다시 확인합니다.",
        "announce_note_2": "설정된 공지 채널과 봇 권한을 검사합니다.",
        "announce_note_3": "성공·건너뜀·실패 결과를 이 화면에 표시합니다.",
        "footer": "{bot_name} local administration · 127.0.0.1:8080",
        # ── 길드 행(Python 조립) ──
        # ── 세션 ──
        "session_remaining": "남은 시간: {hours}시간 {minutes}분 (만료 {clock} KST)",
        # ── 서버 생성 문구 ──
        "guild_row_party_channel": "파티 채널",
        "guild_row_announcement_channel": "공지 채널",
        "guild_row_event_channel": "이벤트 채널",
        "guild_row_web_announce": "웹 공지",
        "guild_row_forbidden_filter": "금지어 필터",
        "guild_row_save": "저장",
        "guild_row_unset": "미지정",
        "guild_row_unrestricted": "제한 없음",
        "guild_row_on": "켜짐",
        "guild_row_off": "꺼짐",
        "guild_row_announce_allowed": "허용",
        "guild_row_announce_blocked": "차단",
        "guild_row_announce_hint": "Discord `/설정 공지허용`에서 변경",
        "guild_row_none": "참여 중인 길드가 없습니다.",
        "channel_no_permission": "권한 부족",
        "channel_deleted": "삭제됨",
        "announce_option_all": "전체 (공지 허용 길드 모두)",
        "reason_timeout": "작업 시간이 초과되었습니다.",
        "reason_db_locked": "설정 DB가 다른 작업에 잠겨 있습니다. 잠시 후 다시 시도해 주세요.",
        "reason_db_readonly": "설정 DB가 읽기 전용입니다. 파일 권한을 확인해 주세요.",
        "reason_db_error": "설정 DB 작업 중 오류가 발생했습니다.",
        "reason_permission": "파일 또는 디렉터리에 접근할 권한이 없습니다.",
        "reason_too_deep": "설정 구조가 너무 깊습니다. 중첩 단계를 줄여 주세요.",
        "reason_invalid_value": "입력값 또는 설정 내용이 올바르지 않습니다.",
        "reason_os_error": "운영체제 작업에 실패했습니다 ({detail}).",
        "reason_discord_forbidden": "Discord가 요청을 거부했습니다. 봇의 채널 권한을 확인해 주세요.",
        "reason_discord_not_found": "대상이 삭제되었거나 더 이상 존재하지 않습니다.",
        "reason_discord_http": "Discord API 요청에 실패했습니다. 잠시 후 다시 시도해 주세요.",
        "reason_unexpected": "예상하지 못한 오류가 발생했습니다 ({kind}).",
        "error_id_not_ascii": "Discord ID는 ASCII 숫자여야 합니다.",
        "error_id_out_of_range": "Discord ID 범위가 올바르지 않습니다.",
        "error_form_urlencoded_only": "application/x-www-form-urlencoded 요청만 허용됩니다.",
        "error_multipart_only": "multipart/form-data 요청만 허용됩니다.",
        "error_bad_request": "잘못된 요청입니다.",
        "error_too_many_fields": "폼 필드가 너무 많습니다.",
        "error_too_many_images": "이미지는 1개만 첨부할 수 있습니다.",
        "error_field_too_large": "폼 필드가 너무 큽니다.",
        "error_image_type": "공지 이미지는 PNG, JPEG, GIF, WebP 파일이어야 합니다.",
        "error_settings_file_not_allowed": "허용되지 않은 설정 파일입니다.",
        "notice_settings_read_failed": "{name}: UTF-8로 읽을 수 없습니다.",
        "notice_guild_read_failed": "길드 설정을 읽지 못했습니다 — {reason}",
        "notice_save_failed": "저장 실패: {reason}",
        "notice_saved_but_reload_failed": "파일은 저장했지만 즉시 반영에 실패했습니다. 원인: {reason}",
        "notice_saved_persona": "저장했습니다. 새 AI 세션부터 적용됩니다.",
        "notice_saved_forbidden": "저장하고 금지어 필터를 다시 불러왔습니다.",
        "notice_saved_games": "저장했습니다. 적용하려면 봇을 재시작하세요.",
        "label_party": "파티",
        "label_announcement": "공지",
        "label_event": "이벤트",
        "notice_guild_value_invalid": "길드 설정 값이 올바르지 않습니다.",
        "notice_channel_value_invalid": "{label} 채널 값이 올바르지 않습니다.",
        "notice_channel_unusable": "{label} 채널을 찾을 수 없거나 봇의 채널 보기, 메시지 보내기, 메시지 기록 보기, 링크 임베드{extra} 권한이 부족합니다.",
        "notice_channel_extra_attach": ", 파일 첨부",
        "notice_guild_save_failed": "길드 설정 저장 실패: {reason}",
        "notice_party_module_missing": "파티 모듈이 로드되지 않아 패널을 갱신하지 못했습니다.",
        "notice_filter_value_invalid": "금지어 필터 값이 올바르지 않습니다.",
        "notice_filter_reload_failed": "금지어 필터 즉시 반영 실패 — {reason}",
        "notice_guild_saved_with_problems": "{guild} 설정은 저장했지만 일부 즉시 반영 작업에 실패했습니다.\n원인: {problems}",
        "notice_guild_saved": "{guild}의 설정을 저장했습니다.",
        "notice_announce_title_length": "공지 제목은 1~256자여야 합니다.",
        "notice_announce_body_length": "공지 본문은 1~4,096자여야 합니다.",
        "notice_announce_target_invalid": "공지 대상 길드가 올바르지 않습니다.",
        "notice_announce_color_format": "공지 색상은 #RRGGBB 형식이어야 합니다.",
        "notice_announce_targets_failed": "공지 대상 목록을 읽지 못했습니다. 원인: {reason}",
        "notice_announce_not_opted_in": "공지를 허용하지 않은 길드입니다.",
        "notice_announce_skip_not_joined": "길드 {guild_id}: 봇이 현재 길드에 접속되어 있지 않아 건너뜀",
        "notice_announce_skip_no_channel": "{guild}: 공지 채널이 지정되지 않아 건너뜀",
        "notice_announce_skip_deleted": "{guild}: 설정된 공지 채널이 삭제되어 건너뜀",
        "notice_announce_skip_permission": "{guild}: 공지 채널의 보기·전송·기록·임베드·파일 첨부 권한이 부족해 건너뜀",
        "notice_announce_failed_one": "{guild}: 공지 처리 실패 — {reason}",
        "notice_announce_guild_fallback": "길드 {guild_id}",
        "notice_announce_summary": "공지 완료: 성공 {success}, 건너뜀 {skipped}, 실패 {failed}",
        "notice_announce_details": "\n원인: {details}",
        "notice_settings_unreadable": "설정을 읽지 못했습니다 — {problems}",
        "notice_guild_not_found": "참여 중인 길드를 찾지 못했습니다.",
        "notice_party_panel_failed": "파티 패널 갱신 실패 — {reason}",
        "error_host_not_allowed": "허용되지 않은 Host입니다.",
        "error_unexpected": "요청 처리에 실패했습니다. 원인: 예상하지 못한 서버 오류 ({kind}). 상세 내용은 봇 콘솔을 확인해 주세요.",
        "error_login_failed": "인증에 실패했습니다.",
        "error_too_many_attempts": "인증 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        "error_login_required": "로그인이 필요합니다.",
        "error_bad_csrf": "잘못된 CSRF 토큰입니다.",
    },
    "en": {
        # ── Shared ──
        "html_lang": "en",
        "login_title": "{bot_name} admin sign-in",
        "index_title": "{bot_name} Bot Control Center",
        "brand_meta": "Bot admin",
        "skip_to_content": "Skip to main content",
        "localhost_protected": "127.0.0.1:8080 · Localhost protected",
        "language_switch_label": "Language",
        "language_korean": "한국어",
        "language_english": "English",
        # ── Sign-in ──
        "login_hero_heading": "Bot Control Center",
        "login_hero_copy": "Manage the persona, content settings, and guild status safely from your local machine.",
        "login_eyebrow": "Administrator access",
        "login_heading": "Administrator sign-in",
        "login_copy": "Enter the admin token you set in the environment variables.",
        "login_token_label": "Admin token",
        "login_token_helper": "The token is never shown on screen outside this sign-in request.",
        "login_submit": "Sign in",
        # ── Sidebar and top bar ──
        "sidebar_label": "Admin menu",
        "nav_section_label": "Admin screen shortcuts",
        "nav_label": "Control center",
        "nav_overview": "Overview",
        "nav_persona": "Persona",
        "nav_content": "Content settings",
        "nav_guilds": "Guilds",
        "nav_announcement": "Announcements",
        "boundary_title": "Localhost protected",
        "boundary_copy": "This admin screen is reachable only at 127.0.0.1:8080.",
        "breadcrumb_console": "Admin console",
        "session_extend": "Extend session",
        "logout": "Sign out",
        # ── Overview ──
        "page_eyebrow": "Local administration",
        "page_title": "Bot Control Center",
        "page_copy": "Manage the persona and content settings, and check the status of the Discord guilds the bot has joined.",
        "status_chip": "ADMIN ONLINE",
        "overview_label": "Admin server status summary",
        "metric_connection": "Connection",
        "metric_connection_value": "Healthy",
        "metric_scope": "Scope",
        "metric_scope_value": "Local",
        "metric_settings_files": "Settings files",
        # ── Bot settings ──
        "settings_title": "Bot settings",
        "settings_copy": "The settings you check most often, in one place.",
        "persona_kicker": "AI identity",
        "persona_title": "Persona",
        "persona_apply": "Applies from the next AI session",
        "persona_prompt_label": "System prompt",
        "persona_prompt_helper": "Up to 16,000 characters",
        "persona_greeting_label": "Greeting",
        "persona_greeting_helper": "Used when a new conversation starts.",
        "persona_submit": "Save persona",
        "required": "required",
        "forbidden_kicker": "Moderation",
        "forbidden_title": "Forbidden words",
        "forbidden_apply": "Applies immediately",
        "forbidden_submit": "Save and reload",
        "games_kicker": "Party finder",
        "games_title": "Game list",
        "games_apply": "Applies after restart",
        "games_submit": "Save game list",
        # ── Guilds ──
        "guilds_title": "Per-guild settings",
        "guilds_copy": "Stores each guild's party, announcement, and event channels, plus whether web announcements and the forbidden-word filter are enabled.",
        "guilds_table_label": "Per-guild channel, announcement, and filter settings",
        "guilds_column_guild": "Guild",
        "guilds_column_settings": "Channels and features",
        # ── Announcements ──
        "announce_title": "Opt-in announcements",
        "announce_copy": "Sends an embed to the designated channel of every guild that opted in.",
        "announce_target": "Target guild",
        "announce_color": "Embed colour",
        "announce_title_label": "Title",
        "announce_body_label": "Body",
        "announce_syntax_summary": "Discord syntax guide",
        "announce_syntax_bold": "bold",
        "announce_syntax_italic": "italic",
        "announce_syntax_underline": "underline",
        "announce_syntax_strike": "strikethrough",
        "announce_syntax_inline": "inline code",
        "announce_syntax_quote": "quote",
        "announce_syntax_list": "list",
        "announce_syntax_link": "link text",
        "announce_syntax_mention": "mention",
        "announce_syntax_helper": "Your line breaks and Discord Markdown are applied to the embed body as written.",
        "announce_image_label": "Image",
        "announce_image_helper": "Optional · PNG, JPEG, GIF, WebP · up to 8 MiB",
        "announce_submit": "Send announcement",
        "announce_note_label": "Announcement checklist",
        "announce_note_title": "Before you send",
        "announce_note_1": "Confirm the guild opted in to host announcements.",
        "announce_note_2": "Check the configured announcement channel and bot permissions.",
        "announce_note_3": "Sent, skipped, and failed results appear on this screen.",
        "footer": "{bot_name} local administration · 127.0.0.1:8080",
        # ── Guild rows (assembled in Python) ──
        # ── Session ──
        "session_remaining": "Time left: {hours}h {minutes}m (expires {clock} KST)",
        # ── Server-generated messages ──
        "guild_row_party_channel": "Party channel",
        "guild_row_announcement_channel": "Announcement channel",
        "guild_row_event_channel": "Event channel",
        "guild_row_web_announce": "Web announcements",
        "guild_row_forbidden_filter": "Forbidden-word filter",
        "guild_row_save": "Save",
        "guild_row_unset": "Not set",
        "guild_row_unrestricted": "No restriction",
        "guild_row_on": "On",
        "guild_row_off": "Off",
        "guild_row_announce_allowed": "Allowed",
        "guild_row_announce_blocked": "Blocked",
        "guild_row_announce_hint": "change with `/설정 공지허용` in Discord",
        "guild_row_none": "The bot has not joined any guild.",
        "channel_no_permission": "missing permissions",
        "channel_deleted": "Deleted",
        "announce_option_all": "All (every opted-in guild)",
        "reason_timeout": "The operation timed out.",
        "reason_db_locked": "The settings database is locked by another operation. Please try again shortly.",
        "reason_db_readonly": "The settings database is read-only. Check the file permissions.",
        "reason_db_error": "The settings database operation failed.",
        "reason_permission": "No permission to access the file or directory.",
        "reason_too_deep": "The settings structure is nested too deeply. Reduce the nesting.",
        "reason_invalid_value": "The input or settings content is not valid.",
        "reason_os_error": "An operating-system call failed ({detail}).",
        "reason_discord_forbidden": "Discord rejected the request. Check the bot's channel permissions.",
        "reason_discord_not_found": "The target was deleted or no longer exists.",
        "reason_discord_http": "The Discord API request failed. Please try again shortly.",
        "reason_unexpected": "An unexpected error occurred ({kind}).",
        "error_id_not_ascii": "A Discord ID must be ASCII digits.",
        "error_id_out_of_range": "The Discord ID is out of range.",
        "error_form_urlencoded_only": "Only application/x-www-form-urlencoded requests are accepted.",
        "error_multipart_only": "Only multipart/form-data requests are accepted.",
        "error_bad_request": "Malformed request.",
        "error_too_many_fields": "Too many form fields.",
        "error_too_many_images": "Only one image can be attached.",
        "error_field_too_large": "A form field is too large.",
        "error_image_type": "The announcement image must be a PNG, JPEG, GIF, or WebP file.",
        "error_settings_file_not_allowed": "That settings file is not allowed.",
        "notice_settings_read_failed": "{name}: cannot be read as UTF-8.",
        "notice_guild_read_failed": "Could not read guild settings — {reason}",
        "notice_save_failed": "Save failed: {reason}",
        "notice_saved_but_reload_failed": "The file was saved but could not be applied immediately. Cause: {reason}",
        "notice_saved_persona": "Saved. It applies from the next AI session.",
        "notice_saved_forbidden": "Saved and reloaded the forbidden-word filter.",
        "notice_saved_games": "Saved. Restart the bot to apply it.",
        "label_party": "Party",
        "label_announcement": "Announcement",
        "label_event": "Event",
        "notice_guild_value_invalid": "The guild setting value is not valid.",
        "notice_channel_value_invalid": "The {label} channel value is not valid.",
        "notice_channel_unusable": "The {label} channel was not found, or the bot lacks View Channel, Send Messages, Read Message History, or Embed Links{extra} permission.",
        "notice_channel_extra_attach": " / Attach Files",
        "notice_guild_save_failed": "Could not save guild settings: {reason}",
        "notice_party_module_missing": "The party module is not loaded, so the panel was not refreshed.",
        "notice_filter_value_invalid": "The forbidden-word filter value is not valid.",
        "notice_filter_reload_failed": "Forbidden-word filter refresh failed — {reason}",
        "notice_guild_saved_with_problems": "{guild}'s settings were saved, but some follow-up actions failed.\nCause: {problems}",
        "notice_guild_saved": "Saved {guild}'s settings.",
        "notice_announce_title_length": "The announcement title must be 1–256 characters.",
        "notice_announce_body_length": "The announcement body must be 1–4,096 characters.",
        "notice_announce_target_invalid": "The announcement target guild is not valid.",
        "notice_announce_color_format": "The announcement colour must be in #RRGGBB format.",
        "notice_announce_targets_failed": "Could not read the announcement target list. Cause: {reason}",
        "notice_announce_not_opted_in": "That guild has not opted in to announcements.",
        "notice_announce_skip_not_joined": "Guild {guild_id}: skipped, the bot is not currently in this guild",
        "notice_announce_skip_no_channel": "{guild}: skipped, no announcement channel is set",
        "notice_announce_skip_deleted": "{guild}: skipped, the configured announcement channel was deleted",
        "notice_announce_skip_permission": "{guild}: skipped, missing View / Send / History / Embed / Attach permission on the announcement channel",
        "notice_announce_failed_one": "{guild}: announcement failed — {reason}",
        "notice_announce_guild_fallback": "Guild {guild_id}",
        "notice_announce_summary": "Announcement finished: {success} sent, {skipped} skipped, {failed} failed",
        "notice_announce_details": "\nCause: {details}",
        "notice_settings_unreadable": "Could not read settings — {problems}",
        "notice_guild_not_found": "No matching guild was found.",
        "notice_party_panel_failed": "Party panel refresh failed — {reason}",
        "error_host_not_allowed": "Host header not allowed.",
        "error_unexpected": "The request failed. Cause: unexpected server error ({kind}). See the bot console for details.",
        "error_login_failed": "Authentication failed.",
        "error_too_many_attempts": "Too many sign-in attempts. Please try again later.",
        "error_login_required": "Sign-in required.",
        "error_bad_csrf": "Invalid CSRF token.",
    },
}


def resolve_language(request) -> str:
    """``?lang=`` → ``admin_lang`` 쿠키 → ``Accept-Language`` → 기본 ``ko``.

    알 수 없는 값은 조용히 기본값으로 떨어진다. 언어 선택은 실패해도 화면이
    떠야 하는 종류의 입력이다.
    """
    # 이 함수는 미들웨어의 500 처리 경로에서도 불린다. 거기서 예외가 나면
    # 원래 오류가 가려지므로, 요청에서 읽는 것은 무엇 하나 필수가 아니다.
    def field(name: str, key: str) -> str:
        container = getattr(request, name, None)
        getter = getattr(container, "get", None)
        return getter(key, "") if getter else ""

    requested = field("query", "lang")
    if requested in SUPPORTED_LANGUAGES:
        return requested

    cookie = field("cookies", LANGUAGE_COOKIE)
    if cookie in SUPPORTED_LANGUAGES:
        return cookie

    for chunk in field("headers", "Accept-Language").split(","):
        tag = chunk.split(";", 1)[0].strip().lower()
        primary = tag.split("-", 1)[0]
        if primary in SUPPORTED_LANGUAGES:
            return primary
    return DEFAULT_LANGUAGE


def chosen_language(request) -> str:
    """``?lang=``로 명시적으로 고른 언어. 없거나 알 수 없으면 빈 문자열.

    쿠키를 구울지 결정할 때 쓴다 — 헤더나 기본값으로 정해진 언어까지 굽으면
    사용자가 고르지도 않은 값이 1년간 남는다.
    """
    query = getattr(request, "query", None)
    getter = getattr(query, "get", None)
    requested = getter("lang", "") if getter else ""
    return requested if requested in SUPPORTED_LANGUAGES else ""


def translate(language: str, key: str, /, **params: object) -> str:
    """카탈로그에서 문구를 꺼내 ``{}`` 자리를 채운다.

    키가 없으면 KeyError가 아니라 키 이름을 그대로 돌려준다 — 관리 화면이
    문구 하나 때문에 500이 되는 것보다는 낫고, 키 누락은 테스트가 잡는다.
    """
    catalog = STRINGS.get(language) or STRINGS[DEFAULT_LANGUAGE]
    template = catalog.get(key) or STRINGS[DEFAULT_LANGUAGE].get(key)
    if template is None:
        return key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template
