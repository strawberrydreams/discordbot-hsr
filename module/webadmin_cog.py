from __future__ import annotations

import asyncio
import html
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
from module.panel import is_sendable_panel_channel


DEFAULT_HOST = "127.0.0.1"
ALLOWED_HOSTS = frozenset({DEFAULT_HOST, "0.0.0.0"})
PORT = 8080
SESSION_COOKIE = "admin_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
# form body는 percent-encoded라 UTF-8 한 바이트가 최대 "%XX" 3자로 늘어난다. 한글
# 설정 파일이 파일 한도 안인데도 413으로 막히면 안 된다. 실제 파일 크기는
# config.atomic_write_settings가 따로 강제하므로 여기는 전송 한도일 뿐이다.
MAX_BODY_BYTES = config.MAX_SETTINGS_BYTES * 3 + 4_096
ANNOUNCEMENT_TITLE_LIMIT = 256
ANNOUNCEMENT_BODY_LIMIT = 4_096
# 길드 하나가 매달려도 나머지 전송과 HTTP 응답까지 끌고 가지 않게 한다.
ANNOUNCE_SEND_TIMEOUT = 10
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
        settings: GuildSettingsRepository | None = None,
    ):
        self.bot = bot
        self.settings = settings or create_guild_settings_repository()
        self.sessions: dict[str, AdminSession] = {}
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._mutation_lock = asyncio.Lock()
        # 공지는 길드 수에 비례해 오래 걸린다. 설정 저장과 lock을 공유하면 공지 한
        # 번이 관리 화면 전체를 막는다.
        self._announce_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self.app = web.Application(
            client_max_size=MAX_BODY_BYTES, middlewares=[_security_headers]
        )
        self.app.add_routes(
            [
                web.get("/login", self.login_get),
                web.post("/login", self.login_post),
                web.post("/logout", self.logout_post),
                web.get("/", self.index_get),
                web.post("/settings/{name}", self.settings_post),
                web.post("/announce", self.announce_post),
            ]
        )

    async def start(self) -> None:
        if self.runner is not None:
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
        self.runner = runner
        self.site = site

    async def close(self) -> None:
        async with self._close_lock:
            runner, self.runner = self.runner, None
            self.site = None
            self.sessions.clear()
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

    def _session(self, request: web.Request) -> tuple[str, AdminSession] | None:
        now = time.monotonic()
        for expired_id in [
            session_id
            for session_id, session in self.sessions.items()
            if session.expires_at <= now
        ]:
            self.sessions.pop(expired_id, None)
        session_id = request.cookies.get(SESSION_COOKIE)
        session = self.sessions.get(session_id or "")
        return (session_id, session) if session_id and session else None

    def _require_session(self, request: web.Request) -> tuple[str, AdminSession]:
        session = self._session(request)
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
    def _template(name: str, **values: str) -> str:
        source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            value = values.get(key, "")
            if key in RAW_PLACEHOLDERS:
                return value
            return html.escape(value, quote=True)

        return PLACEHOLDER_PATTERN.sub(substitute, source)

    def _login_response(self, error: str = "", *, status: int = 200) -> web.Response:
        return web.Response(
            text=self._template("admin_login.html", error=error),
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
            data = config.read_settings_bytes(name)
        except (OSError, ValueError) as exc:
            return "", f"{name}: {exc}"
        if data is None:
            return "", ""
        try:
            return data.decode("utf-8"), ""
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
    def _channel_label(guild, channel_id: int | None) -> str:
        if not channel_id:
            return "미설정"
        channel = guild.get_channel(channel_id)
        if channel is None:
            return f"삭제됨 ({channel_id})"
        label = f"#{channel.name}"
        return label if is_sendable_panel_channel(guild, channel) else f"{label} (권한 부족)"

    async def _guild_overview(self) -> tuple[str, str, str]:
        """(길드 현황 표의 행, 공지 대상 option, 읽기 실패 사유)를 돌려준다.

        운영자는 어느 길드가 공지를 허용했고 패널 채널이 어디인지 확인할 수단이
        없으면 공지를 보내기 전에 대상을 검증할 수 없다.

        ponytail: 길드당 조회 2회다. 1운영자 1인스턴스 규모에서는 무시할 수 있다.
        길드가 수백 개로 늘면 전 길드 설정을 한 번에 읽는 repository 메서드를
        추가한다.
        """
        rows: list[str] = []
        options = ['<option value="">전체 (공지 허용 길드 모두)</option>']
        for guild in self.bot.guilds:
            try:
                party = await run_db(self.settings.get_party_channel, guild.id)
                allowed = await run_db(self.settings.get_allow_host_announce, guild.id)
            except Exception:
                logger.exception("Guild overview lookup failed for guild_id=%s", guild.id)
                return "", "".join(options), "길드 설정을 읽지 못했습니다."
            name = html.escape(str(guild.name), quote=True)
            rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    name,
                    html.escape(self._channel_label(guild, party), quote=True),
                    "허용" if allowed else "차단",
                )
            )
            if allowed:
                options.append(f'<option value="{int(guild.id)}">{name}</option>')
        if not rows:
            rows.append('<tr><td colspan="3">참여 중인 길드가 없습니다.</td></tr>')
        return "".join(rows), "".join(options), ""

    async def _index_response(self, csrf: str, notice: str = "") -> web.Response:
        texts: dict[str, str] = {}
        problems: list[str] = []
        for name in config.SETTINGS_FILES:
            if name == "persona.json":
                continue
            text, problem = self._settings_text(name)
            texts[name] = text
            if problem:
                problems.append(problem)
        persona_prompt, persona_greeting, persona_problem = self._persona_fields()
        if persona_problem:
            problems.append(persona_problem)
        guild_rows, guild_options, guild_problem = await self._guild_overview()
        if guild_problem:
            problems.append(guild_problem)
        if problems:
            warning = "설정을 읽지 못했습니다 — " + " / ".join(problems)
            notice = f"{notice} {warning}" if notice else warning
        return web.Response(
            text=self._template(
                "admin_index.html",
                csrf=csrf,
                notice=notice,
                persona_prompt=persona_prompt,
                persona_greeting=persona_greeting,
                forbidden_words=texts["forbidden_words.json"],
                games=texts["games.json"],
                guild_rows=guild_rows,
                guild_options=guild_options,
            ),
            content_type="text/html",
            charset="utf-8",
        )

    async def login_get(self, request: web.Request) -> web.StreamResponse:
        if self._session(request):
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
        self.sessions.clear()
        self.sessions[session_id] = AdminSession(
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
        self.sessions.pop(session_id, None)
        response = web.HTTPSeeOther("/login")
        response.del_cookie(SESSION_COOKIE, path="/")
        raise response

    async def index_get(self, request: web.Request) -> web.StreamResponse:
        session = self._session(request)
        if session is None:
            raise web.HTTPFound("/login")
        return await self._index_response(session[1].csrf)

    async def settings_post(self, request: web.Request) -> web.StreamResponse:
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        name = request.match_info["name"]
        if name not in config.SETTINGS_FILES:
            raise web.HTTPNotFound(text="허용되지 않은 설정 파일입니다.")
        try:
            if name == "persona.json":
                # 화면이 값 두 개만 받으므로 JSON은 여기서 조립한다. 형식 검증은
                # atomic_write_settings의 canonicalizer가 그대로 수행한다.
                document = {
                    "system_prompt": form.get("system_prompt", ""),
                    "greeting": form.get("greeting", ""),
                }
            else:
                document = json.loads(form.get("document", ""))
            async with self._mutation_lock:
                await asyncio.to_thread(config.atomic_write_settings, name, document)
                if name == "forbidden_words.json":
                    cog = self.bot.get_cog("ForbiddenFilterCog")
                    if cog is not None:
                        await cog.reload_prohibited_words()
        except (OSError, ValueError, RecursionError) as exc:
            return await self._index_response(csrf, f"저장 실패: {exc}")

        notices = {
            "persona.json": "저장했습니다. 새 AI 세션부터 적용됩니다.",
            "forbidden_words.json": "저장하고 금지어 필터를 다시 불러왔습니다.",
            "games.json": "저장했습니다. 적용하려면 봇을 재시작하세요.",
        }
        return await self._index_response(csrf, notices[name])

    async def announce_post(self, request: web.Request) -> web.StreamResponse:
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        title = form.get("title", "").strip()
        body = form.get("body", "").strip()
        target = form.get("guild_id", "").strip()
        colour_text = form.get("color", "").strip()
        if not title or len(title) > ANNOUNCEMENT_TITLE_LIMIT:
            return await self._index_response(csrf, "공지 제목은 1~256자여야 합니다.")
        if not body or len(body) > ANNOUNCEMENT_BODY_LIMIT:
            return await self._index_response(csrf, "공지 본문은 1~4,096자여야 합니다.")
        if target and not target.isdigit():
            return await self._index_response(csrf, "공지 대상 길드가 올바르지 않습니다.")
        colour = discord.Colour.default()
        if colour_text:
            if not ANNOUNCEMENT_COLOUR_PATTERN.match(colour_text):
                return await self._index_response(
                    csrf, "공지 색상은 #RRGGBB 형식이어야 합니다."
                )
            colour = discord.Colour(int(colour_text[1:], 16))

        success = skipped = failed = 0
        async with self._announce_lock:
            try:
                guild_ids = await run_db(self.settings.list_announcement_guild_ids)
            except Exception:
                return await self._index_response(csrf, "공지 대상 목록을 읽지 못했습니다.")
            if target:
                # 옵트인 목록이 곧 신뢰 경계다. 드롭다운 표시 여부와 무관하게
                # 전송 직전에 다시 확인해야 form을 조작한 요청이 통과하지 않는다.
                if int(target) not in guild_ids:
                    return await self._index_response(
                        csrf, "공지를 허용하지 않은 길드입니다."
                    )
                guild_ids = [int(target)]
            embed = discord.Embed(title=title, description=body, colour=colour)
            for guild_id in guild_ids:
                try:
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        skipped += 1
                        continue
                    channel_id = await run_db(self.settings.get_party_channel, guild_id)
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if not is_sendable_panel_channel(guild, channel):
                        skipped += 1
                        continue
                    await asyncio.wait_for(
                        channel.send(embed=embed), timeout=ANNOUNCE_SEND_TIMEOUT
                    )
                    success += 1
                except Exception:
                    failed += 1
                    logger.exception("Host announcement failed for guild_id=%s", guild_id)

        return await self._index_response(
            csrf, f"공지 완료: 성공 {success}, 건너뜀 {skipped}, 실패 {failed}"
        )


async def setup(bot: commands.Bot) -> None:
    cog = WebAdminCog(bot)
    await cog.start()
    try:
        await bot.add_cog(cog)
    except BaseException:
        await cog.close()
        raise
