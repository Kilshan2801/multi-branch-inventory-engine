import logging
import uuid

from fastapi import FastAPI, Request
from sqlalchemy import text

from app.api.routes import auth, users
from app.core.config import get_settings
from app.core.database import engine
from app.core.errors import register_exception_handlers

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

API_PREFIX = "/api/v1"

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "Multi-branch inventory reservation and order processing engine.\n\n"
        "1. Call **POST /api/v1/auth/login** to get an access token.\n"
        "2. Click **Authorize** and paste the token.\n\n"
        'All errors share one shape: `{"error": {"code", "message", "details"}, "request_id"}`.'
    ),
)

register_exception_handlers(app)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Tag each request with an id (client-supplied or generated); it appears in
    logs, audit entries, error bodies and the X-Request-ID response header."""
    incoming = request.headers.get("X-Request-ID", "")
    request.state.request_id = incoming[:64] if incoming else uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
