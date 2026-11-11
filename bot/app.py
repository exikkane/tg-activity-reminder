import asyncio
import fcntl
import logging
import os
import secrets
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings
from .db import DB
from .services import ReminderService, WorkService
from .telegram import UI, escape
from .youtrack import ServiceError, YouTrack

log = logging.getLogger(__name__)


def create_app(settings=None):
    s = settings or Settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx URL logging can expose Telegram bot tokens and OAuth callback parameters.
    for name in ("httpx", "httpcore", "aiogram.event", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    @asynccontextmanager
    async def lifespan(app):
        os.umask(0o077)
        s.db_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(str(s.db_path) + ".lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("Only one application process may use this database") from None
        db = DB(s.db_path, s.token_encryption_key.get_secret_value())
        yt = YouTrack(s, db)
        session = AiohttpSession(proxy=s.telegram_proxy_url.get_secret_value() or None)
        bot = Bot(s.telegram_bot_token.get_secret_value(), session=session)
        work = WorkService(s, db, yt)
        ui = UI(s, db, yt, work, bot)
        reminder = ReminderService(s, db, ui.reminder)
        app.state.db, app.state.yt, app.state.ui = db, yt, ui
        tasks = []

        async def schedule():
            while True:
                await reminder.tick(datetime.now(timezone.utc))
                db.cleanup()
                await asyncio.sleep(30)

        if s.run_bot:
            tasks = [asyncio.create_task(ui.poll()), asyncio.create_task(schedule())]
        app.state.tasks = tasks
        app.state.stopping = False

        def stop_on_failure(task):
            if task.cancelled() or app.state.stopping:
                return
            error = task.exception()
            log.error(
                "background_task_stopped kind=%s",
                type(error).__name__ if error else "unexpected_exit",
            )
            # Trigger Uvicorn graceful shutdown; Compose restart policy restarts the process.
            os.kill(os.getpid(), signal.SIGTERM)

        for task in tasks:
            task.add_done_callback(stop_on_failure)
        try:
            yield
        finally:
            app.state.stopping = True
            # A cancelled in-flight POST stays sending and is reconciled after restart.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await yt.close()
            await bot.session.close()
            db.close()
            lock.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/healthz")
    async def health():
        from fastapi.responses import JSONResponse

        db_ok = app.state.db.one("SELECT 1 AS ok")["ok"] == 1
        ok = db_ok and all(not task.done() for task in app.state.tasks)
        return JSONResponse(
            {"status": "ok" if ok else "failed", "bot_enabled": s.run_bot},
            status_code=200 if ok else 503,
        )

    @app.get("/connect")
    async def connect(ticket: str):
        try:
            if len(ticket) > 100:
                raise ValueError("Неверная ссылка.")
            cookie = secrets.token_urlsafe(32)
            row = app.state.db.start_oauth(ticket, cookie)
            response = RedirectResponse(
                app.state.yt.authorize_url(ticket, app.state.db.open(row["verifier"])),
                status_code=302,
            )
            response.set_cookie(
                "yt_oauth",
                cookie,
                httponly=True,
                secure=not s.allow_local_http,
                samesite="lax",
                max_age=600,
                path=urlparse(s.youtrack_oauth_redirect_uri).path,
            )
            return response
        except ValueError as exc:
            return HTMLResponse(escape(str(exc), 300), status_code=400)

    @app.get("/oauth/callback")
    async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        db, yt, ui = app.state.db, app.state.yt, app.state.ui
        try:
            if not state or len(state) > 100 or len(code) > 4096:
                raise ValueError("Неверный ответ авторизации.")
            row = db.consume_oauth(state, request.cookies.get("yt_oauth", ""))
            tg = row["tg"]
            async with ui.locks[tg]:
                if not s.allowed(tg):
                    raise ValueError("Подключение недоступно.")
                if error or not code:
                    raise ValueError("Подключение отменено. Можно попробовать снова через /start.")
                tokens = await yt.exchange(
                    dict(
                        grant_type="authorization_code",
                        code=code,
                        redirect_uri=s.youtrack_oauth_redirect_uri,
                        code_verifier=db.open(row["verifier"]),
                    )
                )
                me = await yt.me(tokens["access_token"])
                # Disconnect must not resurrect a callback already in flight.
                if not db.one("SELECT state FROM oauth WHERE state=?", (row["state"],)):
                    raise ValueError("Сессия подключения отменена.")
                db.bind(tg, me, tokens, s.bot_timezone)
                try:
                    await ui.show(
                        tg,
                        f"<b>✅ YouTrack подключён!</b>\n\nАккаунт: {escape(me.get('fullName') or me['login'])}\n\nТеперь ты можешь записывать время по задачам прямо здесь.",
                        ui.keys([ui.button(tg, "📋 Мои задачи", "list")]),
                    )
                except Exception as exc:
                    log.warning("oauth_notification_failed kind=%s", type(exc).__name__)
            response = HTMLResponse("YouTrack подключён. Вернись в Telegram и открой /tasks.")
        except (ValueError, ServiceError) as exc:
            response = HTMLResponse(escape(str(exc), 400), status_code=400)
        except Exception as exc:
            log.error("oauth_failed kind=%s", type(exc).__name__)
            response = HTMLResponse(
                "Подключение не удалось. Вернись в Telegram и отправь /start.", status_code=502
            )
        response.delete_cookie("yt_oauth", path=urlparse(s.youtrack_oauth_redirect_uri).path)
        return response

    return app
