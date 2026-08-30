from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import discord
from aiohttp import web
from discord.ext import commands

from module import config
from module.config import KST_TIMEZONE
from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)
from module.forbidden_filter_cog import ForbiddenFilterCog
from module.i18n import (
    LANGUAGE_COOKIE,
    LANGUAGE_COOKIE_MAX_AGE,
    chosen_language,
    resolve_language,
    translate,
)
from module.panel import (
    channel_send_capabilities,
    is_sendable_announcement_channel,
    is_sendable_panel_channel,
)
from module.party_cog import PartyCog

DEFAULT_HOST = "127.0.0.1"
ALLOWED_HOSTS = frozenset({DEFAULT_HOST, "0.0.0.0"})
PORT = 8080
SESSION_COOKIE = "admin_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
PANEL_REFRESH_TIMEOUT_SECONDS = 10
REQUEST_HOSTS = frozenset({"127.0.0.1", "localhost"})
MAX_SNOWFLAKE = (1 << 64) - 1
# form body는 percent-encoded라 UTF-8 한 바이트가 최대 "%XX" 3자로 늘어난다. 한글
# 설정 파일이 파일 한도 안인데도 413으로 막히면 안 된다. 실제 파일 크기는
# config.atomic_write_settings가 따로 강제하므로 여기는 전송 한도일 뿐이다.
MAX_BODY_BYTES = config.MAX_SETTINGS_BYTES * 3 + 4_096
ANNOUNCEMENT_IMAGE_LIMIT = 8 * 1024 * 1024
MAX_REQUEST_BYTES = ANNOUNCEMENT_IMAGE_LIMIT + 64 * 1024
ANNOUNCEMENT_TITLE_LIMIT = 256
ANNOUNCEMENT_BODY_LIMIT = 4_096
# 길드 하나가 매달려도 나머지 전송과 HTTP 응답까지 끌고 가지 않게 한다.
ANNOUNCEMENT_SEND_TIMEOUT_SECONDS = 10
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
ANNOUNCEMENT_COLOUR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}\Z")
TEMPLATE_DIR = Path(__file__).with_name("templates")
STYLESHEET_PATH = Path(__file__).with_name("static") / "admin.css"
PLACEHOLDER_PATTERN = re.compile(
    r"{{(?:t:(?P<key>[a-z0-9_]+)|(?P<value>csrf|notice|error|persona_prompt"
    r"|persona_greeting|forbidden_words|games|guild_options|guild_rows"
    r"|bot_name|bot_name_initial|session_remaining))}}"
)
# 이 둘만 Python에서 조각마다 html.escape()해 조립한 HTML 단편이다. 나머지 값은
# 그대로 escape한다. 여기에 새 키를 넣으려면 조립부가 모든 외부 값을 escape하는지
# 먼저 확인할 것.
RAW_PLACEHOLDERS = frozenset({"guild_options", "guild_rows"})
logger = logging.getLogger(__name__)


class PublicInputError(ValueError):
    """검증된 사용자 입력 오류만 HTTP 응답에 노출한다.

    문구가 아니라 카탈로그 키를 들고 다닌다. 이 예외는 언어를 알 수 없는 깊은
    곳에서 나고, 번역은 응답 직전에 해야 하기 때문이다.
    """

    def __init__(self, key: str, **params: object):
        super().__init__(key)
        self.key = key
        self.params = params


def _request_host_allowed(request: web.Request) -> bool:
    raw_host = request.headers.get("Host", "")
    if not raw_host or "," in raw_host or "@" in raw_host:
        return False
    if raw_host.startswith("["):
        closing = raw_host.find("]")
        host = raw_host[1:closing] if closing > 0 else ""
        remainder = raw_host[closing + 1 :] if closing > 0 else raw_host
        if remainder and not re.fullmatch(r":[0-9]{1,5}", remainder):
            return False
    else:
        if raw_host.count(":") > 1:
            return False
        host, separator, port = raw_host.partition(":")
        if separator and not re.fullmatch(r"[0-9]{1,5}", port):
            return False
    return host.lower() in REQUEST_HOSTS


def _parse_snowflake(raw_value: str, *, optional: bool = False) -> int | None:
    value = raw_value.strip()
    if optional and not value:
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise PublicInputError("error_id_not_ascii")
    parsed = int(value)
    if not 0 < parsed <= MAX_SNOWFLAKE:
        raise PublicInputError("error_id_out_of_range")
    return parsed


def _bind_host() -> str:
    host = os.environ.get("WEB_ADMIN_HOST", DEFAULT_HOST)
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(
            "WEB_ADMIN_HOST는 127.0.0.1 또는 0.0.0.0이어야 합니다."
        )
    return host


@dataclass(frozen=True)
class AdminSession:
    csrf: str
    expires_at: float


@dataclass(frozen=True)
class AnnouncementUpload:
    content_type: str
    payload: bytes


@web.middleware
async def _security_headers(request: web.Request, handler):
    try:
        if not _request_host_allowed(request):
            raise web.HTTPBadRequest(
                text=translate(resolve_language(request), "error_host_not_allowed")
            )
        response = await handler(request)
    except web.HTTPException as response:
        response.headers.update(SECURITY_HEADERS)
        raise
    except Exception as error:
        logger.exception("Unhandled web admin request: %s %s", request.method, request.path)
        response = web.Response(
            status=500,
            text=translate(
                resolve_language(request),
                "error_unexpected",
                kind=type(error).__name__,
            ),
        )
    response.headers.update(SECURITY_HEADERS)
    return response


class WebAdminCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        settings_repository: GuildSettingsRepository | None = None,
    ):
        self.bot = bot
        self.settings_repository = (
            settings_repository or create_guild_settings_repository()
        )
        self.admin_sessions: dict[str, AdminSession] = {}
        self._failed_login_attempts: deque[float] = deque()
        self.web_runner: web.AppRunner | None = None
        self.web_site: web.TCPSite | None = None
        self._mutation_lock = asyncio.Lock()
        # 공지는 길드 수에 비례해 오래 걸린다. 설정 저장과 lock을 공유하면 공지 한
        # 번이 관리 화면 전체를 막는다.
        self._announcement_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self.app = web.Application(
            client_max_size=MAX_REQUEST_BYTES, middlewares=[_security_headers]
        )
        self.app.add_routes(
            [
                web.get("/static/admin.css", self.stylesheet_get),
                web.get("/login", self.login_get),
                web.post("/login", self.login_post),
                web.post("/logout", self.logout_post),
                web.post("/session/extend", self.session_extend_post),
                web.get("/", self.index_get),
                web.post("/settings/{name}", self.settings_post),
                web.post("/guilds/settings", self.guild_settings_post),
                web.post("/announce", self.announce_post),
            ]
        )

    async def start(self) -> None:
        if self.web_runner is not None:
            return
        host = _bind_host()
        runner = web.AppRunner(self.app)
        try:
            await runner.setup()
            site = web.TCPSite(runner, host, PORT)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self.web_runner = runner
        self.web_site = site

    async def close(self) -> None:
        async with self._close_lock:
            runner, self.web_runner = self.web_runner, None
            self.web_site = None
            self.admin_sessions.clear()
            if runner is not None:
                await runner.cleanup()

    async def cog_unload(self) -> None:
        await self.close()

    async def _read_form(self, request: web.Request) -> dict[str, str]:
        lang = resolve_language(request)
        if request.content_type != "application/x-www-form-urlencoded":
            raise web.HTTPUnsupportedMediaType(
                text=translate(lang, "error_form_urlencoded_only")
            )
        length = request.content_length
        if length is not None and length > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BODY_BYTES, actual_size=length
            )
        body = await request.read()
        if len(body) > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BODY_BYTES, actual_size=len(body)
            )
        try:
            text = body.decode("utf-8")
            if re.search(r"%(?![0-9A-Fa-f]{2})", text):
                raise ValueError("invalid percent escape")
            parsed = parse_qs(
                text,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=10,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(
                text=translate(lang, "error_bad_request")
            ) from exc
        return {key: values[-1] for key, values in parsed.items()}

    async def _read_announcement_form(
        self, request: web.Request
    ) -> tuple[dict[str, str], AnnouncementUpload | None]:
        lang = resolve_language(request)
        if request.content_type == "application/x-www-form-urlencoded":
            return await self._read_form(request), None
        if request.content_type != "multipart/form-data":
            raise web.HTTPUnsupportedMediaType(
                text=translate(lang, "error_multipart_only")
            )

        form: dict[str, str] = {}
        upload: AnnouncementUpload | None = None
        image_seen = False
        field_count = 0
        reader = await request.multipart()
        while part := await reader.next():
            field_count += 1
            if field_count > 10:
                raise web.HTTPBadRequest(
                    text=translate(lang, "error_too_many_fields")
                )
            if part.name == "image":
                if image_seen:
                    raise web.HTTPBadRequest(
                        text=translate(lang, "error_too_many_images")
                    )
                image_seen = True
                payload = bytearray()
                while chunk := await part.read_chunk():
                    payload.extend(chunk)
                    if len(payload) > ANNOUNCEMENT_IMAGE_LIMIT:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=ANNOUNCEMENT_IMAGE_LIMIT,
                            actual_size=len(payload),
                        )
                if payload:
                    upload = AnnouncementUpload(
                        content_type=part.headers.get("Content-Type", "").lower(),
                        payload=bytes(payload),
                    )
                continue

            payload = await part.read(decode=True)
            if len(payload) > 16 * 1024:
                raise web.HTTPBadRequest(
                    text=translate(lang, "error_field_too_large")
                )
            try:
                form[part.name or ""] = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise web.HTTPBadRequest(
                    text=translate(lang, "error_bad_request")
                ) from exc
        return form, upload

    @staticmethod
    def _announcement_image_name(upload: AnnouncementUpload) -> str:
        image_signatures = {
            "image/png": (
                "png",
                lambda image_data: image_data.startswith(b"\x89PNG\r\n\x1a\n"),
            ),
            "image/jpeg": (
                "jpg",
                lambda image_data: image_data.startswith(b"\xff\xd8\xff"),
            ),
            "image/gif": (
                "gif",
                lambda image_data: image_data.startswith((b"GIF87a", b"GIF89a")),
            ),
            "image/webp": (
                "webp",
                lambda image_data: len(image_data) >= 12
                and image_data.startswith(b"RIFF")
                and image_data[8:12] == b"WEBP",
            ),
        }
        extension_and_check = image_signatures.get(upload.content_type)
        if extension_and_check is None or not extension_and_check[1](upload.payload):
            raise PublicInputError(
                "error_image_type"
            )
        return f"announcement.{extension_and_check[0]}"

    def _get_active_session(
        self, request: web.Request
    ) -> tuple[str, AdminSession] | None:
        current_time = time.monotonic()
        for expired_id in [
            session_id
            for session_id, session in self.admin_sessions.items()
            if session.expires_at <= current_time
        ]:
            self.admin_sessions.pop(expired_id, None)
        session_id = request.cookies.get(SESSION_COOKIE)
        session = self.admin_sessions.get(session_id or "")
        return (session_id, session) if session_id and session else None

    def _session_remaining_text(self, lang: str, csrf: str) -> str:
        """'남은 시간: 7시간 52분 (만료 21:14 KST)'. 세션이 없으면 빈 문자열.

        초 단위로 줄어드는 시계가 아니라 페이지를 다시 열 때 갱신되는 값이다.
        CSP가 default-src 'self'라 인라인 스크립트가 막혀 있고, 카운트다운
        하나 때문에 CSP를 넓히지 않기로 했다. 연장 버튼은 303으로 되돌아오므로
        누르면 갱신된 값이 바로 보인다.
        """
        session = next(
            (s for s in self.admin_sessions.values() if s.csrf == csrf), None
        )
        if session is None:
            return ""
        remaining = max(0, int(session.expires_at - time.monotonic()))
        hours, minutes = divmod(remaining // 60, 60)
        expires_at = datetime.now(KST_TIMEZONE) + timedelta(seconds=remaining)
        return translate(
            lang,
            "session_remaining",
            hours=hours,
            minutes=minutes,
            clock=expires_at.strftime("%H:%M"),
        )

    def _require_session(self, request: web.Request) -> tuple[str, AdminSession]:
        session = self._get_active_session(request)
        if session is None:
            raise web.HTTPUnauthorized(
                text=translate(resolve_language(request), "error_login_required")
            )
        return session

    def _require_csrf(
        self, request: web.Request, form: dict[str, str]
    ) -> tuple[str, AdminSession]:
        session = self._require_session(request)
        supplied = form.get("csrf", "")
        if not supplied or not secrets.compare_digest(
            supplied.encode(), session[1].csrf.encode()
        ):
            raise web.HTTPForbidden(
                text=translate(resolve_language(request), "error_bad_csrf")
            )
        return session

    @staticmethod
    def _render_template(name: str, *, lang: str, **values: str) -> str:
        template_source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")

        def substitute(match: re.Match[str]) -> str:
            key = match.group("key")
            if key is not None:
                # 카탈로그 값도 기존 규칙대로 escape한다. RAW는 여기 없다.
                return html.escape(
                    translate(lang, key, bot_name=values.get("bot_name", "")),
                    quote=True,
                )
            placeholder_name = match.group("value")
            placeholder_value = values.get(placeholder_name, "")
            if placeholder_name in RAW_PLACEHOLDERS:
                return placeholder_value
            return html.escape(placeholder_value, quote=True)

        return PLACEHOLDER_PATTERN.sub(substitute, template_source)

    def _bot_name(self) -> str:
        """Discord Application에 등록된 봇 이름.

        웹서버는 로그인 전에 뜨지만 페이지는 요청 시점에 렌더되고 그때는 이미
        READY다. READY 직전 몇 초에 열면 폴백이 보인다. 길드별 닉네임이 아니라
        전역 이름을 쓴다 — 이 화면은 길드에 매여 있지 않다.
        """
        user = getattr(self.bot, "user", None)
        return getattr(user, "display_name", None) or "Bot"

    async def stylesheet_get(self, request: web.Request) -> web.StreamResponse:
        """고정 경로 하나만 서빙한다. web.static()을 쓰지 않으므로 경로 조작이 없다.

        로그인 화면도 이 스타일시트를 쓰므로 인증 밖에 둔다.
        """
        return web.Response(
            body=STYLESHEET_PATH.read_bytes(),
            content_type="text/css",
            charset="utf-8",
        )

    def _login_response(
        self, lang: str, error: str = "", *, status: int = 200
    ) -> web.Response:
        return web.Response(
            text=self._render_template(
                "admin_login.html",
                lang=lang,
                error=error,
                bot_name=self._bot_name(),
                bot_name_initial=self._bot_name()[:1],
            ),
            content_type="text/html",
            charset="utf-8",
            status=status,
        )

    @staticmethod
    def _settings_text(lang: str, name: str) -> tuple[str, str]:
        """(현재 내용, 읽기 실패 사유)를 돌려준다.

        빈 문자열만 돌려주면 파일이 없는 것과 못 읽는 것이 화면에서 구분되지
        않는다. SETTINGS_DIR이 group 쓰기 가능하거나 소유자가 다르면
        read_settings_bytes()가 PermissionError를 내는데, 그 이유를 삼키면
        운영자는 빈 textarea만 보고 원인을 알 수 없다.
        """
        try:
            settings_bytes = config.read_settings_bytes(name)
        except (OSError, ValueError) as exc:
            return "", f"{name}: {exc}"
        if settings_bytes is None:
            return "", ""
        try:
            return settings_bytes.decode("utf-8"), ""
        except UnicodeDecodeError:
            return "", translate(lang, "notice_settings_read_failed", name=name)

    @classmethod
    def _persona_fields(cls, lang: str) -> tuple[str, str, str]:
        """(system_prompt, greeting, 읽기 실패 사유)를 돌려준다.

        persona는 문자열 두 개뿐이라 JSON 원문을 그대로 편집하게 하면 따옴표·
        줄바꿈 이스케이프 실수로 저장이 거부되기만 한다. 화면에서는 값만 다루고
        JSON 조립은 서버가 한다. 저장 시 config.atomic_write_settings가 같은
        canonicalizer로 다시 검증하므로 검증 경로는 하나다.
        """
        text, problem = cls._settings_text(lang, "persona.json")
        if problem or not text:
            return "", "", problem
        # config._canonicalize_settings와 같은 이유로 함수 안에서 import한다.
        from module.ai_chat_cog import canonicalize_persona

        try:
            persona = canonicalize_persona(json.loads(text), strict=True)
        except (ValueError, TypeError) as exc:
            return "", "", f"persona.json: {exc}"
        return persona["system_prompt"], persona["greeting"], ""

    @staticmethod
    def _channel_options(
        lang: str,
        channels: list[tuple[object, bool, bool]],
        selected_channel_id: int | None,
        *,
        unset_label: str,
        announcement: bool = False,
    ) -> str:
        options = [
            '<option value=""{}>{}</option>'.format(
                " selected" if selected_channel_id is None else "",
                html.escape(unset_label, quote=True),
            )
        ]
        selected_channel_found = False
        for channel, panel_allowed, announcement_allowed in channels:
            channel_id = int(channel.id)
            selected = channel_id == selected_channel_id
            selected_channel_found |= selected
            label = f"#{channel.name}"
            usable = announcement_allowed if announcement else panel_allowed
            if not usable:
                label += f" ({translate(lang, 'channel_no_permission')})"
            options.append(
                '<option value="{}"{}>{}</option>'.format(
                    channel_id,
                    " selected" if selected else "",
                    html.escape(label, quote=True),
                )
            )
        if selected_channel_id and not selected_channel_found:
            options.append(
                '<option value="{}" selected>{} ({})</option>'.format(
                    int(selected_channel_id),
                    html.escape(translate(lang, "channel_deleted"), quote=True),
                    int(selected_channel_id),
                )
            )
        return "".join(options)

    async def _guild_overview(self, lang: str, csrf: str) -> tuple[str, str, str]:
        """(길드 현황 표의 행, 공지 대상 option, 읽기 실패 사유)를 돌려준다.

        운영자는 어느 길드가 공지를 허용했고 공지 채널이 어디인지 확인할 수단이
        없으면 공지를 보내기 전에 대상을 검증할 수 없다.

        ponytail: 길드당 조회 5회다. 1운영자 1인스턴스 규모에서는 무시할 수 있다.
        길드가 수백 개로 늘면 전 길드 설정을 한 번에 읽는 repository 메서드를
        추가한다.
        """
        escaped = lambda key: html.escape(translate(lang, key), quote=True)
        label_party = escaped("guild_row_party_channel")
        label_announcement = escaped("guild_row_announcement_channel")
        label_event = escaped("guild_row_event_channel")
        label_web_announce = escaped("guild_row_web_announce")
        label_filter = escaped("guild_row_forbidden_filter")
        label_save = escaped("guild_row_save")
        label_unset = translate(lang, "guild_row_unset")
        label_unrestricted = translate(lang, "guild_row_unrestricted")
        label_on = escaped("guild_row_on")
        label_off = escaped("guild_row_off")
        label_allowed = escaped("guild_row_announce_allowed")
        label_blocked = escaped("guild_row_announce_blocked")
        label_announce_hint = escaped("guild_row_announce_hint")

        row_fragments: list[str] = []
        option_fragments = [
            '<option value="">{}</option>'.format(
                html.escape(translate(lang, "announce_option_all"), quote=True)
            )
        ]
        for guild in self.bot.guilds:
            try:
                party_channel_id = await run_db(
                    self.settings_repository.get_party_channel, guild.id
                )
                announcement_channel_id = await run_db(
                    self.settings_repository.get_announcement_channel, guild.id
                )
                event_channel_id = await run_db(
                    self.settings_repository.get_event_channel, guild.id
                )
                announcements_allowed = await run_db(
                    self.settings_repository.get_allow_host_announce, guild.id
                )
                forbidden_filter_enabled = await run_db(
                    self.settings_repository.get_forbidden_filter_enabled,
                    guild.id,
                )
            except Exception as error:
                logger.exception("Guild overview lookup failed for guild_id=%s", guild.id)
                return (
                    "",
                    "".join(option_fragments),
                    translate(
                        lang,
                        "notice_guild_read_failed",
                        reason=self._operation_error_reason(lang, error),
                    ),
                )
            escaped_guild_name = html.escape(str(guild.name), quote=True)
            channels = [
                (channel, *channel_send_capabilities(guild, channel))
                for channel in getattr(guild, "text_channels", ())
            ]
            settings_form = (
                '<form class="guild-settings-form" method="post" '
                'action="/guilds/settings">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
                f'<input type="hidden" name="guild_id" value="{int(guild.id)}">'
                '<div class="guild-settings-fields">'
                f'<label>{label_party}'
                f'<select name="party_channel_id" aria-label="{escaped_guild_name} {label_party}">'
                f'{self._channel_options(lang, channels, party_channel_id, unset_label=label_unset)}'
                '</select><button class="button button-secondary" name="setting" '
                f'value="party_channel_id" type="submit">{label_save}</button></label>'
                f'<label>{label_announcement}'
                f'<select name="announcement_channel_id" aria-label="{escaped_guild_name} {label_announcement}">'
                f'{self._channel_options(lang, channels, announcement_channel_id, unset_label=label_unset, announcement=True)}'
                '</select><button class="button button-secondary" name="setting" '
                f'value="announcement_channel_id" type="submit">{label_save}</button></label>'
                f'<label>{label_event}'
                f'<select name="event_channel_id" aria-label="{escaped_guild_name} {label_event}">'
                f'{self._channel_options(lang, channels, event_channel_id, unset_label=label_unrestricted)}'
                '</select><button class="button button-secondary" name="setting" '
                f'value="event_channel_id" type="submit">{label_save}</button></label>'
                f'<label>{label_web_announce} '
                f'<span>{label_allowed if announcements_allowed else label_blocked} — '
                f'{label_announce_hint}</span></label>'
                f'<label>{label_filter}'
                f'<select name="forbidden_filter_enabled" aria-label="{escaped_guild_name} {label_filter}">'
                f'<option value="1"{" selected" if forbidden_filter_enabled else ""}>{label_on}</option>'
                f'<option value="0"{"" if forbidden_filter_enabled else " selected"}>{label_off}</option>'
                '</select><button class="button button-secondary" name="setting" '
                f'value="forbidden_filter_enabled" type="submit">{label_save}</button></label>'
                '</div>'
                '</form>'
            )
            row_fragments.append(
                "<tr><td>{}</td><td>{}</td></tr>".format(
                    escaped_guild_name,
                    settings_form,
                )
            )
            if announcements_allowed:
                option_fragments.append(
                    f'<option value="{int(guild.id)}">{escaped_guild_name}</option>'
                )
        if not row_fragments:
            row_fragments.append(
                '<tr><td colspan="2">{}</td></tr>'.format(
                    html.escape(translate(lang, "guild_row_none"), quote=True)
                )
            )
        return "".join(row_fragments), "".join(option_fragments), ""

    @staticmethod
    def _operation_error_reason(lang: str, error: Exception) -> str:
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return translate(lang, "reason_timeout")
        if type(error).__module__ == "sqlite3":
            message = str(error).lower()
            if "locked" in message or "busy" in message:
                return translate(lang, "reason_db_locked")
            if "readonly" in message:
                return translate(lang, "reason_db_readonly")
            return translate(lang, "reason_db_error")
        if isinstance(error, PermissionError):
            return translate(lang, "reason_permission")
        if isinstance(error, RecursionError):
            return translate(lang, "reason_too_deep")
        if isinstance(error, PublicInputError):
            return translate(lang, error.key, **error.params)
        if isinstance(error, ValueError):
            return translate(lang, "reason_invalid_value")
        if isinstance(error, OSError):
            return translate(
                lang,
                "reason_os_error",
                detail=error.strerror or type(error).__name__,
            )
        if isinstance(error, discord.Forbidden):
            return translate(lang, "reason_discord_forbidden")
        if isinstance(error, discord.NotFound):
            return translate(lang, "reason_discord_not_found")
        if isinstance(error, discord.HTTPException):
            return translate(lang, "reason_discord_http")
        return translate(lang, "reason_unexpected", kind=type(error).__name__)

    async def _index_response(
        self, lang: str, csrf: str, notice: str = ""
    ) -> web.Response:
        settings_text_by_name: dict[str, str] = {}
        settings_problems: list[str] = []
        for name in config.SETTINGS_FILES:
            if name == "persona.json":
                continue
            text, problem = self._settings_text(lang, name)
            settings_text_by_name[name] = text
            if problem:
                settings_problems.append(problem)
        persona_prompt, persona_greeting, persona_problem = self._persona_fields(
            lang
        )
        if persona_problem:
            settings_problems.append(persona_problem)
        guild_rows, guild_options, guild_problem = await self._guild_overview(
            lang, csrf
        )
        if guild_problem:
            settings_problems.append(guild_problem)
        if settings_problems:
            warning = translate(
                lang,
                "notice_settings_unreadable",
                problems=" / ".join(settings_problems),
            )
            notice = f"{notice} {warning}" if notice else warning
        return web.Response(
            text=self._render_template(
                "admin_index.html",
                lang=lang,
                csrf=csrf,
                notice=notice,
                persona_prompt=persona_prompt,
                persona_greeting=persona_greeting,
                forbidden_words=settings_text_by_name["forbidden_words.json"],
                games=settings_text_by_name["games.json"],
                guild_rows=guild_rows,
                guild_options=guild_options,
                bot_name=self._bot_name(),
                bot_name_initial=self._bot_name()[:1],
                session_remaining=self._session_remaining_text(lang, csrf),
            ),
            content_type="text/html",
            charset="utf-8",
        )

    @staticmethod
    def _remember_language(response: web.StreamResponse, request) -> web.StreamResponse:
        """?lang= 로 고른 언어를 쿠키에 남긴다.

        POST 뒤 리다이렉트되는 화면에는 쿼리가 남지 않으므로, 쿠키가 없으면
        저장 한 번마다 언어가 기본값으로 되돌아간다.
        """
        chosen = chosen_language(request)
        if chosen:
            response.set_cookie(
                LANGUAGE_COOKIE,
                chosen,
                httponly=True,
                samesite="Lax",
                path="/",
                max_age=LANGUAGE_COOKIE_MAX_AGE,
            )
        return response

    async def login_get(self, request: web.Request) -> web.StreamResponse:
        if self._get_active_session(request):
            raise web.HTTPFound("/")
        return self._remember_language(
            self._login_response(resolve_language(request)), request
        )

    async def login_post(self, request: web.Request) -> web.StreamResponse:
        lang = resolve_language(request)
        form = await self._read_form(request)
        supplied = form.get("token", "")
        expected = config.ADMIN_TOKEN or ""
        now = time.monotonic()
        cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
        while self._failed_login_attempts and self._failed_login_attempts[0] <= cutoff:
            self._failed_login_attempts.popleft()
        if len(self._failed_login_attempts) >= LOGIN_FAILURE_LIMIT:
            response = self._login_response(
                lang,
                translate(lang, "error_too_many_attempts"),
                status=429,
            )
            response.headers["Retry-After"] = str(LOGIN_FAILURE_WINDOW_SECONDS)
            return response
        if not supplied or not expected or not secrets.compare_digest(
            supplied.encode(), expected.encode()
        ):
            self._failed_login_attempts.append(now)
            return self._login_response(
                lang, translate(lang, "error_login_failed"), status=401
            )

        self._failed_login_attempts.clear()
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self.admin_sessions.clear()
        self.admin_sessions[session_id] = AdminSession(
            csrf=csrf, expires_at=time.monotonic() + SESSION_TTL_SECONDS
        )
        response = web.HTTPSeeOther("/")
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            samesite="Strict",
            path="/",
            max_age=SESSION_TTL_SECONDS,
        )
        raise response

    async def session_extend_post(self, request: web.Request) -> web.StreamResponse:
        """세션 만료를 다시 TTL만큼 뒤로 민다.

        연장 횟수 상한은 두지 않는다. 이 화면은 loopback 전용이고 세션도 항상
        하나뿐이라, 상한은 작업 중 강제 로그아웃이라는 더 나쁜 실패를 만든다.
        """
        self._require_session(request)
        form = await self._read_form(request)
        session_id, session = self._require_csrf(request, form)
        self.admin_sessions[session_id] = replace(
            session, expires_at=time.monotonic() + SESSION_TTL_SECONDS
        )
        response = web.HTTPSeeOther("/")
        # 쿠키 만료도 함께 밀어야 브라우저가 먼저 세션을 버리지 않는다.
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            samesite="Strict",
            path="/",
            max_age=SESSION_TTL_SECONDS,
        )
        raise response

    async def logout_post(self, request: web.Request) -> web.StreamResponse:
        self._require_session(request)
        form = await self._read_form(request)
        session_id, _ = self._require_csrf(request, form)
        self.admin_sessions.pop(session_id, None)
        response = web.HTTPSeeOther("/login")
        response.del_cookie(SESSION_COOKIE, path="/")
        raise response

    async def index_get(self, request: web.Request) -> web.StreamResponse:
        session = self._get_active_session(request)
        if session is None:
            raise web.HTTPFound("/login")
        return self._remember_language(
            await self._index_response(resolve_language(request), session[1].csrf),
            request,
        )

    async def settings_post(self, request: web.Request) -> web.StreamResponse:
        lang = resolve_language(request)
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        settings_filename = request.match_info["name"]
        if settings_filename not in config.SETTINGS_FILES:
            raise web.HTTPNotFound(
                text=translate(lang, "error_settings_file_not_allowed")
            )
        if settings_filename == "persona.json":
            # 화면이 값 두 개만 받으므로 JSON은 여기서 조립한다. 형식 검증은
            # atomic_write_settings의 canonicalizer가 그대로 수행한다.
            document = {
                "system_prompt": form.get("system_prompt", ""),
                "greeting": form.get("greeting", ""),
            }
        else:
            try:
                document = json.loads(form.get("document", ""))
            except ValueError as error:
                return await self._index_response(
                    lang,
                    csrf,
                    translate(
                        lang,
                        "notice_save_failed",
                        reason=self._operation_error_reason(lang, error),
                    ),
                )
        settings_saved = False
        try:
            async with self._mutation_lock:
                await asyncio.to_thread(
                    config.atomic_write_settings, settings_filename, document
                )
                settings_saved = True
                if settings_filename == "forbidden_words.json":
                    forbidden_filter_cog = self.bot.get_cog(
                        ForbiddenFilterCog.__name__
                    )
                    if forbidden_filter_cog is not None:
                        await forbidden_filter_cog.reload_forbidden_words()
        except (OSError, ValueError, RecursionError) as error:
            if settings_saved:
                return await self._index_response(
                    lang,
                    csrf,
                    translate(
                        lang,
                        "notice_saved_but_reload_failed",
                        reason=self._operation_error_reason(lang, error),
                    ),
                )
            return await self._index_response(
                lang,
                csrf,
                translate(
                    lang,
                    "notice_save_failed",
                    reason=self._operation_error_reason(lang, error),
                ),
            )
        except Exception as error:
            logger.exception("Web settings file save failed: %s", settings_filename)
            if settings_saved:
                return await self._index_response(
                    lang,
                    csrf,
                    translate(
                        lang,
                        "notice_saved_but_reload_failed",
                        reason=self._operation_error_reason(lang, error),
                    ),
                )
            return await self._index_response(
                lang,
                csrf,
                translate(
                    lang,
                    "notice_save_failed",
                    reason=self._operation_error_reason(lang, error),
                ),
            )

        notices = {
            "persona.json": "notice_saved_persona",
            "forbidden_words.json": "notice_saved_forbidden",
            "games.json": "notice_saved_games",
        }
        return self._remember_language(
            await self._index_response(
                lang, csrf, translate(lang, notices[settings_filename])
            ),
            request,
        )

    async def guild_settings_post(self, request: web.Request) -> web.StreamResponse:
        lang = resolve_language(request)
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        setting_name = form.get("setting", "")
        channel_settings = {
            "party_channel_id": (
                translate(lang, "label_party"),
                self.settings_repository.set_party_channel,
                False,
            ),
            "announcement_channel_id": (
                translate(lang, "label_announcement"),
                self.settings_repository.set_announcement_channel,
                True,
            ),
            "event_channel_id": (
                translate(lang, "label_event"),
                self.settings_repository.set_event_channel,
                False,
            ),
        }
        if setting_name not in {
            *channel_settings,
            "forbidden_filter_enabled",
        }:
            return await self._index_response(
                lang, csrf, translate(lang, "notice_guild_value_invalid")
            )

        try:
            guild_id = _parse_snowflake(form.get("guild_id", ""))
        except PublicInputError:
            return await self._index_response(
                lang, csrf, translate(lang, "notice_guild_value_invalid")
            )
        assert guild_id is not None
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return await self._index_response(
                lang, csrf, translate(lang, "notice_guild_not_found")
            )

        side_effect_problems: list[str] = []
        if setting_name in channel_settings:
            label, setter, announcement = channel_settings[setting_name]
            try:
                channel_id = _parse_snowflake(
                    form.get(setting_name, ""), optional=True
                )
            except PublicInputError:
                return await self._index_response(
                    lang,
                    csrf,
                    translate(lang, "notice_channel_value_invalid", label=label),
                )
            if channel_id is not None:
                channel = guild.get_channel(channel_id)
                usable = (
                    is_sendable_announcement_channel(guild, channel)
                    if announcement
                    else is_sendable_panel_channel(guild, channel)
                )
                if not usable:
                    extra = (
                        translate(lang, "notice_channel_extra_attach")
                        if announcement
                        else ""
                    )
                    return await self._index_response(
                        lang,
                        csrf,
                        translate(
                            lang,
                            "notice_channel_unusable",
                            label=label,
                            extra=extra,
                        ),
                    )
            try:
                async with self._mutation_lock:
                    await run_db(setter, guild_id, channel_id)
            except Exception as error:
                logger.exception(
                    "Guild channel setting save failed for guild_id=%s setting=%s",
                    guild_id,
                    setting_name,
                )
                return await self._index_response(
                    lang,
                    csrf,
                    translate(
                        lang,
                        "notice_guild_save_failed",
                        reason=self._operation_error_reason(lang, error),
                    ),
                )

            if setting_name == "party_channel_id" and channel_id is not None:
                play_with_cog = self.bot.get_cog(PartyCog.__name__)
                if play_with_cog is None:
                    side_effect_problems.append(
                        translate(lang, "notice_party_module_missing")
                    )
                else:
                    try:
                        await asyncio.wait_for(
                            play_with_cog.ensure_panels(guild),
                            timeout=PANEL_REFRESH_TIMEOUT_SECONDS,
                        )
                    except Exception as error:
                        logger.exception(
                            "Party panel refresh failed for guild_id=%s", guild_id
                        )
                        side_effect_problems.append(
                            translate(
                                lang,
                                "notice_party_panel_failed",
                                reason=self._operation_error_reason(lang, error),
                            )
                        )
        else:
            filter_text = form.get("forbidden_filter_enabled", "")
            if filter_text not in {"0", "1"}:
                return await self._index_response(
                    lang, csrf, translate(lang, "notice_filter_value_invalid")
                )
            try:
                async with self._mutation_lock:
                    await run_db(
                        self.settings_repository.set_forbidden_filter_enabled,
                        guild_id,
                        filter_text == "1",
                    )
            except Exception as error:
                logger.exception(
                    "Forbidden filter setting save failed for guild_id=%s", guild_id
                )
                return await self._index_response(
                    lang,
                    csrf,
                    translate(
                        lang,
                        "notice_guild_save_failed",
                        reason=self._operation_error_reason(lang, error),
                    ),
                )

            forbidden_filter_cog = self.bot.get_cog(ForbiddenFilterCog.__name__)
            if forbidden_filter_cog is not None:
                try:
                    forbidden_filter_cog.invalidate_guild(guild_id)
                except Exception as error:
                    logger.exception(
                        "Forbidden filter cache refresh failed for guild_id=%s",
                        guild_id,
                    )
                    side_effect_problems.append(
                        translate(
                            lang,
                            "notice_filter_reload_failed",
                            reason=self._operation_error_reason(lang, error),
                        )
                    )

        if side_effect_problems:
            return self._remember_language(
                await self._index_response(
                    lang,
                    csrf,
                    translate(
                        lang,
                        "notice_guild_saved_with_problems",
                        guild=guild.name,
                        problems=" / ".join(side_effect_problems),
                    ),
                ),
                request,
            )
        return self._remember_language(
            await self._index_response(
                lang,
                csrf,
                translate(lang, "notice_guild_saved", guild=guild.name),
            ),
            request,
        )

    async def announce_post(self, request: web.Request) -> web.StreamResponse:
        lang = resolve_language(request)
        self._require_session(request)
        form, upload = await self._read_announcement_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        title = form.get("title", "").strip()
        body = form.get("body", "").strip()
        target_guild_id_text = form.get("guild_id", "").strip()
        colour_text = form.get("color", "").strip()
        try:
            image_name = self._announcement_image_name(upload) if upload else None
        except PublicInputError as exc:
            return await self._index_response(
                lang, csrf, translate(lang, exc.key, **exc.params)
            )
        if not title or len(title) > ANNOUNCEMENT_TITLE_LIMIT:
            return await self._index_response(
                lang, csrf, translate(lang, "notice_announce_title_length")
            )
        if not body or len(body) > ANNOUNCEMENT_BODY_LIMIT:
            return await self._index_response(
                lang, csrf, translate(lang, "notice_announce_body_length")
            )
        try:
            target_guild_id = _parse_snowflake(
                target_guild_id_text, optional=True
            )
        except PublicInputError:
            return await self._index_response(
                lang, csrf, translate(lang, "notice_announce_target_invalid")
            )
        colour = discord.Colour.default()
        if colour_text:
            if not ANNOUNCEMENT_COLOUR_PATTERN.match(colour_text):
                return await self._index_response(
                    lang, csrf, translate(lang, "notice_announce_color_format")
                )
            colour = discord.Colour(int(colour_text[1:], 16))

        success_count = skipped_count = failed_count = 0
        outcome_details: list[str] = []
        async with self._announcement_lock:
            try:
                guild_ids = await run_db(
                    self.settings_repository.list_announcement_guild_ids
                )
            except Exception as error:
                logger.exception("Announcement target lookup failed")
                return await self._index_response(
                    lang,
                    csrf,
                    translate(
                        lang,
                        "notice_announce_targets_failed",
                        reason=self._operation_error_reason(lang, error),
                    ),
                )
            if target_guild_id is not None:
                # 옵트인 목록이 곧 신뢰 경계다. 드롭다운 표시 여부와 무관하게
                # 전송 직전에 다시 확인해야 form을 조작한 요청이 통과하지 않는다.
                if target_guild_id not in guild_ids:
                    return await self._index_response(
                        lang, csrf, translate(lang, "notice_announce_not_opted_in")
                    )
                guild_ids = [target_guild_id]
            embed = discord.Embed(title=title, description=body, colour=colour)
            if image_name:
                embed.set_image(url=f"attachment://{image_name}")
            for guild_id in guild_ids:
                guild = None
                try:
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        skipped_count += 1
                        outcome_details.append(
                            translate(
                                lang,
                                "notice_announce_skip_not_joined",
                                guild_id=guild_id,
                            )
                        )
                        continue
                    channel_id = await run_db(
                        self.settings_repository.get_announcement_channel,
                        guild_id,
                    )
                    if not channel_id:
                        skipped_count += 1
                        outcome_details.append(
                            translate(
                                lang,
                                "notice_announce_skip_no_channel",
                                guild=guild.name,
                            )
                        )
                        continue
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        skipped_count += 1
                        outcome_details.append(
                            translate(
                                lang,
                                "notice_announce_skip_deleted",
                                guild=guild.name,
                            )
                        )
                        continue
                    if not is_sendable_announcement_channel(guild, channel):
                        skipped_count += 1
                        outcome_details.append(
                            translate(
                                lang,
                                "notice_announce_skip_permission",
                                guild=guild.name,
                            )
                        )
                        continue
                    announcement_file = (
                        discord.File(io.BytesIO(upload.payload), filename=image_name)
                        if upload and image_name
                        else None
                    )
                    try:
                        send_request = (
                            channel.send(embed=embed, file=announcement_file)
                            if announcement_file is not None
                            else channel.send(embed=embed)
                        )
                        await asyncio.wait_for(
                            send_request,
                            timeout=ANNOUNCEMENT_SEND_TIMEOUT_SECONDS,
                        )
                    finally:
                        if announcement_file is not None:
                            announcement_file.close()
                    success_count += 1
                except Exception as error:
                    failed_count += 1
                    logger.exception("Host announcement failed for guild_id=%s", guild_id)
                    guild_label = (
                        guild.name
                        if guild is not None
                        else translate(
                            lang,
                            "notice_announce_guild_fallback",
                            guild_id=guild_id,
                        )
                    )
                    outcome_details.append(
                        translate(
                            lang,
                            "notice_announce_failed_one",
                            guild=guild_label,
                            reason=self._operation_error_reason(lang, error),
                        )
                    )

        notice = translate(
            lang,
            "notice_announce_summary",
            success=success_count,
            skipped=skipped_count,
            failed=failed_count,
        )
        if outcome_details:
            notice += translate(
                lang,
                "notice_announce_details",
                details=" / ".join(outcome_details),
            )
        return self._remember_language(
            await self._index_response(lang, csrf, notice), request
        )


async def setup(bot: commands.Bot) -> None:
    web_admin_cog = WebAdminCog(bot)
    await web_admin_cog.start()
    try:
        await bot.add_cog(web_admin_cog)
    except BaseException:
        await web_admin_cog.close()
        raise
