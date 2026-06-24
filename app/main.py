from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import db_session, init_db
from app.models import Config
from app.routers import admin, feed, feedback, ongoing, reader, websub
from app import scheduler


@asynccontextmanager
async def lifespan(_app: FastAPI):
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
app.include_router(admin.router)
app.include_router(ongoing.router)
app.include_router(websub.router)


@app.get("/robots.txt", include_in_schema=False)
def robots() -> FileResponse:
    return FileResponse("app/static/robots.txt", media_type="text/plain")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/")
