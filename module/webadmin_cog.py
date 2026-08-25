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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

import discord
from aiohttp import web
from discord.ext import commands

from module import config
from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)
from module.panel import is_sendable_announcement_channel, is_sendable_panel_channel


DEFAULT_HOST = "127.0.0.1"
ALLOWED_HOSTS = frozenset({DEFAULT_HOST, "0.0.0.0"})
PORT = 8080
SESSION_COOKIE = "admin_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
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
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
ANNOUNCEMENT_COLOUR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}\Z")
TEMPLATE_DIR = Path(__file__).with_name("templates")
PLACEHOLDER_PATTERN = re.compile(
    r"{{(csrf|notice|error|persona_prompt|persona_greeting|forbidden_words"
    r"|games|guild_options|guild_rows)}}"
)
# 이 둘만 Python에서 조각마다 html.escape()해 조립한 HTML 단편이다. 나머지 값은
# 그대로 escape한다. 여기에 새 키를 넣으려면 조립부가 모든 외부 값을 escape하는지
# 먼저 확인할 것.
RAW_PLACEHOLDERS = frozenset({"guild_options", "guild_rows"})
logger = logging.getLogger(__name__)


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
        response = await handler(request)
    except web.HTTPException as response:
        response.headers.update(SECURITY_HEADERS)
        raise
    except Exception:
        logger.exception("Unhandled web admin request: %s %s", request.method, request.path)
        response = web.Response(status=500, text="서버 오류가 발생했습니다.")
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
                web.get("/login", self.login_get),
                web.post("/login", self.login_post),
                web.post("/logout", self.logout_post),
                web.get("/", self.index_get),
                web.post("/settings/{name}", self.settings_post),
                web.post("/guilds/event-channel", self.event_channel_post),
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
        if request.content_type != "application/x-www-form-urlencoded":
            raise web.HTTPUnsupportedMediaType(
                text="application/x-www-form-urlencoded 요청만 허용됩니다."
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
                raise ValueError("잘못된 percent escape")
            parsed = parse_qs(
                text,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=10,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="잘못된 요청입니다.") from exc
        return {key: values[-1] for key, values in parsed.items()}

    async def _read_announcement_form(
        self, request: web.Request
    ) -> tuple[dict[str, str], AnnouncementUpload | None]:
        if request.content_type == "application/x-www-form-urlencoded":
            return await self._read_form(request), None
        if request.content_type != "multipart/form-data":
            raise web.HTTPUnsupportedMediaType(
                text="multipart/form-data 요청만 허용됩니다."
            )

        form: dict[str, str] = {}
        upload: AnnouncementUpload | None = None
        image_seen = False
        field_count = 0
        reader = await request.multipart()
        while part := await reader.next():
            field_count += 1
            if field_count > 10:
                raise web.HTTPBadRequest(text="폼 필드가 너무 많습니다.")
            if part.name == "image":
                if image_seen:
                    raise web.HTTPBadRequest(text="이미지는 1개만 첨부할 수 있습니다.")
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
                raise web.HTTPBadRequest(text="폼 필드가 너무 큽니다.")
            try:
                form[part.name or ""] = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise web.HTTPBadRequest(text="잘못된 요청입니다.") from exc
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
            raise ValueError("공지 이미지는 PNG, JPEG, GIF, WebP 파일이어야 합니다.")
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

    def _require_session(self, request: web.Request) -> tuple[str, AdminSession]:
        session = self._get_active_session(request)
        if session is None:
            raise web.HTTPUnauthorized(text="로그인이 필요합니다.")
        return session

    def _require_csrf(
        self, request: web.Request, form: dict[str, str]
    ) -> tuple[str, AdminSession]:
        session = self._require_session(request)
        supplied = form.get("csrf", "")
        if not supplied or not secrets.compare_digest(
            supplied.encode(), session[1].csrf.encode()
        ):
            raise web.HTTPForbidden(text="잘못된 CSRF 토큰입니다.")
        return session

    @staticmethod
    def _render_template(name: str, **values: str) -> str:
        template_source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")

        def substitute(match: re.Match[str]) -> str:
            placeholder_name = match.group(1)
            placeholder_value = values.get(placeholder_name, "")
            if placeholder_name in RAW_PLACEHOLDERS:
                return placeholder_value
            return html.escape(placeholder_value, quote=True)

        return PLACEHOLDER_PATTERN.sub(substitute, template_source)

    def _login_response(self, error: str = "", *, status: int = 200) -> web.Response:
        return web.Response(
            text=self._render_template("admin_login.html", error=error),
            content_type="text/html",
            charset="utf-8",
            status=status,
        )

    @staticmethod
    def _settings_text(name: str) -> tuple[str, str]:
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
            return "", f"{name}: UTF-8로 읽을 수 없습니다."

    @classmethod
    def _persona_fields(cls) -> tuple[str, str, str]:
        """(system_prompt, greeting, 읽기 실패 사유)를 돌려준다.

        persona는 문자열 두 개뿐이라 JSON 원문을 그대로 편집하게 하면 따옴표·
        줄바꿈 이스케이프 실수로 저장이 거부되기만 한다. 화면에서는 값만 다루고
        JSON 조립은 서버가 한다. 저장 시 config.atomic_write_settings가 같은
        canonicalizer로 다시 검증하므로 검증 경로는 하나다.
        """
        text, problem = cls._settings_text("persona.json")
        if problem or not text:
            return "", "", problem
        # config._canonicalize_settings와 같은 이유로 함수 안에서 import한다.
        from module.hyacine_chat_cog import canonicalize_persona

        try:
            persona = canonicalize_persona(json.loads(text), strict=True)
        except (ValueError, TypeError) as exc:
            return "", "", f"persona.json: {exc}"
        return persona["system_prompt"], persona["greeting"], ""

    @staticmethod
    def _channel_label(
        guild, channel_id: int | None, *, announcement: bool = False
    ) -> str:
        if not channel_id:
            return "미설정"
        channel = guild.get_channel(channel_id)
        if channel is None:
            return f"삭제됨 ({channel_id})"
        label = f"#{channel.name}"
        usable = (
            is_sendable_announcement_channel(guild, channel)
            if announcement
            else is_sendable_panel_channel(guild, channel)
        )
        return label if usable else f"{label} (권한 부족)"

    @staticmethod
    def _event_channel_options(guild, selected_channel_id: int | None) -> str:
        options = [
            '<option value=""{}>제한 없음</option>'.format(
                " selected" if selected_channel_id is None else ""
            )
        ]
        selected_channel_found = False
        for channel in getattr(guild, "text_channels", ()):
            channel_id = int(channel.id)
            selected = channel_id == selected_channel_id
            selected_channel_found |= selected
            label = f"#{channel.name}"
            if not is_sendable_panel_channel(guild, channel):
                label += " (권한 부족)"
            options.append(
                '<option value="{}"{}>{}</option>'.format(
                    channel_id,
                    " selected" if selected else "",
                    html.escape(label, quote=True),
                )
            )
        if selected_channel_id and not selected_channel_found:
            options.append(
                '<option value="{}" selected>삭제됨 ({})</option>'.format(
                    int(selected_channel_id), int(selected_channel_id)
                )
            )
        return "".join(options)

    async def _guild_overview(self, csrf: str) -> tuple[str, str, str]:
        """(길드 현황 표의 행, 공지 대상 option, 읽기 실패 사유)를 돌려준다.

        운영자는 어느 길드가 공지를 허용했고 공지 채널이 어디인지 확인할 수단이
        없으면 공지를 보내기 전에 대상을 검증할 수 없다.

        ponytail: 길드당 조회 4회다. 1운영자 1인스턴스 규모에서는 무시할 수 있다.
        길드가 수백 개로 늘면 전 길드 설정을 한 번에 읽는 repository 메서드를
        추가한다.
        """
        row_fragments: list[str] = []
        option_fragments = [
            '<option value="">전체 (공지 허용 길드 모두)</option>'
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
            except Exception:
                logger.exception("Guild overview lookup failed for guild_id=%s", guild.id)
                return (
                    "",
                    "".join(option_fragments),
                    "길드 설정을 읽지 못했습니다.",
                )
            escaped_guild_name = html.escape(str(guild.name), quote=True)
            event_channel_form = (
                '<form class="channel-form" method="post" '
                'action="/guilds/event-channel">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
                f'<input type="hidden" name="guild_id" value="{int(guild.id)}">'
                f'<select name="channel_id" aria-label="{escaped_guild_name} 이벤트 채널">'
                f'{self._event_channel_options(guild, event_channel_id)}'
                '</select><button class="button button-secondary" type="submit">저장</button>'
                '</form>'
            )
            row_fragments.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    escaped_guild_name,
                    html.escape(
                        self._channel_label(guild, party_channel_id), quote=True
                    ),
                    html.escape(
                        self._channel_label(
                            guild,
                            announcement_channel_id,
                            announcement=True,
                        ),
                        quote=True,
                    ),
                    event_channel_form,
                    "허용" if announcements_allowed else "차단",
                )
            )
            if announcements_allowed:
                option_fragments.append(
                    f'<option value="{int(guild.id)}">{escaped_guild_name}</option>'
                )
        if not row_fragments:
            row_fragments.append(
                '<tr><td colspan="5">참여 중인 길드가 없습니다.</td></tr>'
            )
        return "".join(row_fragments), "".join(option_fragments), ""

    async def _index_response(self, csrf: str, notice: str = "") -> web.Response:
        settings_text_by_name: dict[str, str] = {}
        settings_problems: list[str] = []
        for name in config.SETTINGS_FILES:
            if name == "persona.json":
                continue
            text, problem = self._settings_text(name)
            settings_text_by_name[name] = text
            if problem:
                settings_problems.append(problem)
        persona_prompt, persona_greeting, persona_problem = self._persona_fields()
        if persona_problem:
            settings_problems.append(persona_problem)
        guild_rows, guild_options, guild_problem = await self._guild_overview(csrf)
        if guild_problem:
            settings_problems.append(guild_problem)
        if settings_problems:
            warning = "설정을 읽지 못했습니다 — " + " / ".join(
                settings_problems
            )
            notice = f"{notice} {warning}" if notice else warning
        return web.Response(
            text=self._render_template(
                "admin_index.html",
                csrf=csrf,
                notice=notice,
                persona_prompt=persona_prompt,
                persona_greeting=persona_greeting,
                forbidden_words=settings_text_by_name["forbidden_words.json"],
                games=settings_text_by_name["games.json"],
                guild_rows=guild_rows,
                guild_options=guild_options,
            ),
            content_type="text/html",
            charset="utf-8",
        )

    async def login_get(self, request: web.Request) -> web.StreamResponse:
        if self._get_active_session(request):
            raise web.HTTPFound("/")
        return self._login_response()

    async def login_post(self, request: web.Request) -> web.StreamResponse:
        form = await self._read_form(request)
        supplied = form.get("token", "")
        expected = config.ADMIN_TOKEN or ""
        if not supplied or not expected or not secrets.compare_digest(
            supplied.encode(), expected.encode()
        ):
            return self._login_response("인증에 실패했습니다.", status=401)

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
        return await self._index_response(session[1].csrf)

    async def settings_post(self, request: web.Request) -> web.StreamResponse:
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        settings_filename = request.match_info["name"]
        if settings_filename not in config.SETTINGS_FILES:
            raise web.HTTPNotFound(text="허용되지 않은 설정 파일입니다.")
        try:
            if settings_filename == "persona.json":
                # 화면이 값 두 개만 받으므로 JSON은 여기서 조립한다. 형식 검증은
                # atomic_write_settings의 canonicalizer가 그대로 수행한다.
                document = {
                    "system_prompt": form.get("system_prompt", ""),
                    "greeting": form.get("greeting", ""),
                }
            else:
                document = json.loads(form.get("document", ""))
            async with self._mutation_lock:
                await asyncio.to_thread(
                    config.atomic_write_settings, settings_filename, document
                )
                if settings_filename == "forbidden_words.json":
                    forbidden_filter_cog = self.bot.get_cog(
                        "ForbiddenFilterCog"
                    )
                    if forbidden_filter_cog is not None:
                        await forbidden_filter_cog.reload_forbidden_words()
        except (OSError, ValueError, RecursionError) as exc:
            return await self._index_response(csrf, f"저장 실패: {exc}")

        notices = {
            "persona.json": "저장했습니다. 새 AI 세션부터 적용됩니다.",
            "forbidden_words.json": "저장하고 금지어 필터를 다시 불러왔습니다.",
            "games.json": "저장했습니다. 적용하려면 봇을 재시작하세요.",
        }
        return await self._index_response(csrf, notices[settings_filename])

    async def event_channel_post(self, request: web.Request) -> web.StreamResponse:
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        guild_id_text = form.get("guild_id", "").strip()
        channel_id_text = form.get("channel_id", "").strip()
        if not guild_id_text.isdigit() or (
            channel_id_text and not channel_id_text.isdigit()
        ):
            return await self._index_response(csrf, "이벤트 채널 설정이 올바르지 않습니다.")

        guild_id = int(guild_id_text)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return await self._index_response(csrf, "참여 중인 길드를 찾지 못했습니다.")

        channel_id = int(channel_id_text) if channel_id_text else None
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if channel is None or not is_sendable_panel_channel(guild, channel):
                return await self._index_response(
                    csrf,
                    "이벤트 채널에는 봇의 채널 보기, 메시지 보내기, 메시지 기록 보기, 링크 임베드 권한이 필요합니다.",
                )

        async with self._mutation_lock:
            await run_db(
                self.settings_repository.set_event_channel,
                guild_id,
                channel_id,
            )
        notice = (
            f"{guild.name}의 `/이벤트` 채널을 <#{channel_id}>로 지정했습니다."
            if channel_id is not None
            else f"{guild.name}의 `/이벤트` 채널 제한을 해제했습니다."
        )
        return await self._index_response(csrf, notice)

    async def announce_post(self, request: web.Request) -> web.StreamResponse:
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
        except ValueError as exc:
            return await self._index_response(csrf, str(exc))
        if not title or len(title) > ANNOUNCEMENT_TITLE_LIMIT:
            return await self._index_response(csrf, "공지 제목은 1~256자여야 합니다.")
        if not body or len(body) > ANNOUNCEMENT_BODY_LIMIT:
            return await self._index_response(csrf, "공지 본문은 1~4,096자여야 합니다.")
        if target_guild_id_text and not target_guild_id_text.isdigit():
            return await self._index_response(csrf, "공지 대상 길드가 올바르지 않습니다.")
        colour = discord.Colour.default()
        if colour_text:
            if not ANNOUNCEMENT_COLOUR_PATTERN.match(colour_text):
                return await self._index_response(
                    csrf, "공지 색상은 #RRGGBB 형식이어야 합니다."
                )
            colour = discord.Colour(int(colour_text[1:], 16))

        success_count = skipped_count = failed_count = 0
        async with self._announcement_lock:
            try:
                guild_ids = await run_db(
                    self.settings_repository.list_announcement_guild_ids
                )
            except Exception:
                return await self._index_response(csrf, "공지 대상 목록을 읽지 못했습니다.")
            if target_guild_id_text:
                # 옵트인 목록이 곧 신뢰 경계다. 드롭다운 표시 여부와 무관하게
                # 전송 직전에 다시 확인해야 form을 조작한 요청이 통과하지 않는다.
                if int(target_guild_id_text) not in guild_ids:
                    return await self._index_response(
                        csrf, "공지를 허용하지 않은 길드입니다."
                    )
                guild_ids = [int(target_guild_id_text)]
            embed = discord.Embed(title=title, description=body, colour=colour)
            if image_name:
                embed.set_image(url=f"attachment://{image_name}")
            for guild_id in guild_ids:
                try:
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        skipped_count += 1
                        continue
                    channel_id = await run_db(
                        self.settings_repository.get_announcement_channel,
                        guild_id,
                    )
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if not is_sendable_announcement_channel(guild, channel):
                        skipped_count += 1
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
                except Exception:
                    failed_count += 1
                    logger.exception("Host announcement failed for guild_id=%s", guild_id)

        return await self._index_response(
            csrf,
            f"공지 완료: 성공 {success_count}, 건너뜀 {skipped_count}, 실패 {failed_count}",
        )


async def setup(bot: commands.Bot) -> None:
    web_admin_cog = WebAdminCog(bot)
    await web_admin_cog.start()
    try:
        await bot.add_cog(web_admin_cog)
    except BaseException:
        await web_admin_cog.close()
        raise
