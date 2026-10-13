import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import admin, feed, feedback, media, reader, websub
from app import scheduler
from app.security import ProxyTokenMiddleware, log_startup_posture


def _configure_logging() -> None:
    """Set the app's log level from BEACON_LOG_LEVEL (default INFO).

    Set BEACON_LOG_LEVEL=DEBUG to trace the WebSub subscribe → verify → store → push
    flow (app/routers/websub.py, app/websub/publisher.py). We raise only the `app`
    logger so uvicorn's access/error logs — and third-party libraries — keep their own level.
    Nothing later may reconfigure logging: init_db()'s Alembic run is told not to
    (database._alembic_config), which is what used to silence everything after startup.
    """
    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=logging.WARNING)  # ensure a root handler exists
    logging.getLogger("app").setLevel(level)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessLogSecretFilter) for f in access.filters):
        access.addFilter(_AccessLogSecretFilter())


# Secrets that ride in request URLs: the global feed secret (`?token=` on every feed/topic URL),
# image signatures (`?sig=`), and the per-drop feedback token in `/fb/{token}`. The journal is
# shipped off-host, so the access log must not carry them.
_URL_SECRET_RE = re.compile(r"([?&](?:token|sig)=|/fb/(?:confirm/)?)[^&/?\s\"]+")


def _mask_url_secrets(text: str) -> str:
    return _URL_SECRET_RE.sub(r"\1***", text)


class _AccessLogSecretFilter(logging.Filter):
    """Mask URL secrets in uvicorn access records (args: client, method, path, version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_mask_url_secrets(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = _mask_url_secrets(record.msg)
        return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    log_startup_posture()
    init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


# No auto-generated API docs: /docs, /redoc and /openapi.json would enumerate every admin route
# to unauthenticated callers.
app = FastAPI(title="Fic Beacon", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(ProxyTokenMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(feed.router)
app.include_router(feedback.router)
app.include_router(reader.router)
app.include_router(media.router)
app.include_router(admin.router)
app.include_router(websub.router)


@app.get("/robots.txt", include_in_schema=False)
def robots() -> FileResponse:
    return FileResponse("app/static/robots.txt", media_type="text/plain")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/")
