from __future__ import annotations

import asyncio
import html
import json
import logging
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


HOST = "127.0.0.1"
PORT = 8080
SESSION_COOKIE = "admin_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_BODY_BYTES = config.MAX_SETTINGS_BYTES
ANNOUNCEMENT_TITLE_LIMIT = 256
ANNOUNCEMENT_BODY_LIMIT = 4_096
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
TEMPLATE_DIR = Path(__file__).with_name("templates")
PLACEHOLDER_PATTERN = re.compile(
    r"{{(csrf|notice|error|persona|forbidden_words|games)}}"
)
logger = logging.getLogger(__name__)


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
        runner = web.AppRunner(self.app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, HOST, PORT)
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
        return PLACEHOLDER_PATTERN.sub(
            lambda match: html.escape(values.get(match.group(1), ""), quote=True),
            source,
        )

    def _login_response(self, error: str = "", *, status: int = 200) -> web.Response:
        return web.Response(
            text=self._template("admin_login.html", error=error),
            content_type="text/html",
            charset="utf-8",
            status=status,
        )

    @staticmethod
    def _settings_text(name: str) -> str:
        try:
            data = config.read_settings_bytes(name)
        except (OSError, ValueError):
            return ""
        if data is None:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    def _index_response(self, csrf: str, notice: str = "") -> web.Response:
        return web.Response(
            text=self._template(
                "admin_index.html",
                csrf=csrf,
                notice=notice,
                persona=self._settings_text("persona.json"),
                forbidden_words=self._settings_text("forbidden_words.json"),
                games=self._settings_text("games.json"),
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
        return self._index_response(session[1].csrf)

    async def settings_post(self, request: web.Request) -> web.StreamResponse:
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        name = request.match_info["name"]
        if name not in config.SETTINGS_FILES:
            raise web.HTTPNotFound(text="허용되지 않은 설정 파일입니다.")
        try:
            document = json.loads(form.get("document", ""))
            async with self._mutation_lock:
                await asyncio.to_thread(config.atomic_write_settings, name, document)
                if name == "forbidden_words.json":
                    cog = self.bot.get_cog("ForbiddenFilterCog")
                    if cog is not None:
                        cog.load_prohibited_words()
        except (OSError, ValueError, RecursionError) as exc:
            return self._index_response(csrf, f"저장 실패: {exc}")

        notices = {
            "persona.json": "저장했습니다. 새 AI 세션부터 적용됩니다.",
            "forbidden_words.json": "저장하고 금지어 필터를 다시 불러왔습니다.",
            "games.json": "저장했습니다. 적용하려면 봇을 재시작하세요.",
        }
        return self._index_response(csrf, notices[name])

    @staticmethod
    def _sendable_text_channel(guild, channel) -> bool:
        if channel is None or getattr(channel, "type", None) not in {
            discord.ChannelType.text,
            discord.ChannelType.news,
        }:
            return False
        member = getattr(guild, "me", None)
        if member is None:
            return False
        permissions = channel.permissions_for(member)
        return all(
            getattr(permissions, name, False)
            for name in ("view_channel", "send_messages", "embed_links")
        )

    async def announce_post(self, request: web.Request) -> web.StreamResponse:
        self._require_session(request)
        form = await self._read_form(request)
        _, session = self._require_csrf(request, form)
        csrf = session.csrf
        title = form.get("title", "").strip()
        body = form.get("body", "").strip()
        if not title or len(title) > ANNOUNCEMENT_TITLE_LIMIT:
            return self._index_response(csrf, "공지 제목은 1~256자여야 합니다.")
        if not body or len(body) > ANNOUNCEMENT_BODY_LIMIT:
            return self._index_response(csrf, "공지 본문은 1~4,096자여야 합니다.")

        success = skipped = failed = 0
        async with self._mutation_lock:
            try:
                guild_ids = await run_db(self.settings.list_announcement_guild_ids)
            except Exception:
                return self._index_response(csrf, "공지 대상 목록을 읽지 못했습니다.")
            embed = discord.Embed(title=title, description=body)
            for guild_id in guild_ids:
                try:
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        skipped += 1
                        continue
                    channel_id = await run_db(self.settings.get_party_channel, guild_id)
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if not self._sendable_text_channel(guild, channel):
                        skipped += 1
                        continue
                    await channel.send(embed=embed)
                    success += 1
                except Exception:
                    failed += 1
                    logger.exception("Host announcement failed for guild_id=%s", guild_id)

        return self._index_response(
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
