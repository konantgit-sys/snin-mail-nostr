"""Nostr Mail — веб-клиент (пакет).

create_app(): FastAPI + роутеры + мост (lifespan) + статика + оптимизации
(GZip, Cache-Control для статики). Точка входа: app.py → create_app().
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import PUBKEY, NPUB, STATIC_DIR
from .bridge import init_bridge
from .db import connect
from .routers import mail, blossom, imap

_APP_START = time.time()


class CacheControlMiddleware:
    """Статика (*.css, *.js, *.png…) — кэш 1 час; всё остальное — no-cache."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                path = scope.get("path", "")
                if path.startswith("/static/app."):  # бандл с fingerprint → immutable
                    headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                elif path.startswith("/static/"):
                    headers.append((b"cache-control", b"public, max-age=3600"))
                else:
                    headers.append((b"cache-control", b"no-cache"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app() -> FastAPI:
    app = FastAPI(title="Nostr Mail", docs_url=None, redoc_url=None)

    # GZip НЕ включаем: внешний прокси *.v2.site сам сжимает ответы,
    # двойное сжатие обрезает поток (проверено 2026-08-26).
    app.add_middleware(CacheControlMiddleware)

    # CORS для статических дашбордов (cryter-dash.v2.site и dev-поддомены)
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://cryter-dash.v2.site",
            "https://dev-cryter-dash.v2.site",
            "https://snin-dashboard.v2.site",
            "https://dev-snin-dashboard.v2.site",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup():
        from .auth import ensure_seed_accounts, sync_owners_from_accounts
        from . import config as _cfg
        ensure_seed_accounts(_cfg.ACCOUNTS_FILE)
        sync_owners_from_accounts()  # все аккаунты → владельцы моста (после рестарта)
        if os.environ.get("MAIL_BRIDGE_EXTERNAL") != "1":
            init_bridge()  # мост отдельным процессом (--workers N) — в воркерах не дублируем

    # статика (кэш заголовками Cache-Control, см. middleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route("/", methods=["GET", "HEAD"])
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    # ── Фаза 0: /api/health — метрики из реальности (cgroup, БД, аптайм) ──
    @app.get("/api/health")
    def health():
        from . import config as _cfg
        mem = {}
        for name, f in (("current", "memory.current"), ("max", "memory.max")):
            try:
                with open(f"/sys/fs/cgroup/{f}") as fh:
                    mem[name] = int(fh.read().strip())
            except Exception:
                mem[name] = None
        db_size = wal_size = 0
        try:
            db_size = os.path.getsize(_cfg.DB)
            wal_path = _cfg.DB + "-wal"
            wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        except Exception:
            pass
        try:
            with connect(_cfg.DB) as conn:
                mails = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
                outbox = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        except Exception:
            mails = outbox = accounts = -1
        mem_pct = None
        if mem.get("current") and mem.get("max"):
            mem_pct = round(100 * mem["current"] / mem["max"], 1)
        # Фаза 4: очередь писем (подписчик → очередь → воркеры)
        try:
            from . import queue as _queue
            mq = _queue.metrics()
        except Exception:
            mq = {}
        return {
            "ok": True,
            "service": "snin-mail",
            "uptime_s": round(time.time() - _APP_START, 1),
            "ram": mem,
            "ram_pct": mem_pct,
            "db_size": db_size,
            "wal_size": wal_size,
            "counters": {"inbox": mails, "outbox": outbox, "accounts": accounts},
            "mail_queue": mq,
        }

    # API-роутеры (включая NIP-05 discovery и Blossom NIP-96)
    app.include_router(mail.router)
    app.include_router(blossom.router)
    app.include_router(imap.router)

    return app
