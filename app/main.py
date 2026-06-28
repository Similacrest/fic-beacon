import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import db_session, init_db
from app.models import Config
from app.routers import admin, feed, feedback, media, ongoing, reader, websub
from app import scheduler


def _configure_logging() -> None:
    """Set the app's log level from BEACON_LOG_LEVEL (default INFO).

    Set BEACON_LOG_LEVEL=DEBUG to trace the WebSub subscribe → verify → store → push
    flow (app/routers/websub.py, app/websub/publisher.py). We raise only the `app`
    logger so uvicorn's access/error logs keep their own level.
    """
    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level)  # ensure a root handler exists
    logging.getLogger("app").setLevel(level)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    init_db()
    with db_session() as session:
        cfg = session.get(Config, 1)
        cadence = cfg.cadence_cron if cfg else settings.default_cadence_cron
    scheduler.start(cadence)
    yield
    scheduler.shutdown()


app = FastAPI(title="Fic Beacon", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(feed.router)
app.include_router(feedback.router)
app.include_router(reader.router)
app.include_router(media.router)
app.include_router(admin.router)
app.include_router(ongoing.router)
app.include_router(websub.router)


@app.get("/robots.txt", include_in_schema=False)
def robots() -> FileResponse:
    return FileResponse("app/static/robots.txt", media_type="text/plain")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/")
