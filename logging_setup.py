"""Structured logging: request-id threading and environment-controlled level.

Every request gets a request id (client-supplied X-Request-ID when valid, else
a generated one). The id is attached to every log record CREATED while the
request is being handled, and echoed back to the client in the X-Request-ID
response header so server logs and clients can be correlated.

The attachment happens via a LogRecordFactory — set at record creation time —
rather than a logger/handler filter: logger filters only run for the emitting
logger (root's filters never see child-logger records) and handler filters
miss test capture handlers, so a factory is the only mechanism that reaches
every record on every path.

Configure with FEEDCHO_LOG_LEVEL (default INFO). Single mode behavior is
unchanged apart from the richer log format.
"""

import contextvars
import logging
import os

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

_configured = False


def _record_factory(*args, **kwargs):
    record = logging._orig_factory(*args, **kwargs)  # noqa: SLF001
    record.request_id = request_id_var.get("-")
    return record


def setup_logging() -> None:
    """Configure logging once per process (idempotent).

    Called at module import in app.py (not from the lifespan). No-ops after
    the first call so repeated TestClient startups in one pytest process
    don't stack handlers. Replaces root handlers: the app owns its process,
    so any prior uvicorn/journald handler config is intentionally discarded
    in favor of the structured format.
    """
    global _configured
    if _configured:
        return
    level_name = os.environ.get("FEEDCHO_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Every LogRecord gets request_id at creation, on every logger/handler
    # path (including pytest's capture handlers). Records created via
    # logging.makeLogRecord (socket/queue receivers) bypass the factory and
    # would fail formatting; unreachable in this codebase, documented.
    if not hasattr(logging, "_orig_factory"):
        logging._orig_factory = logging.getLogRecordFactory()
        logging.setLogRecordFactory(_record_factory)
    # httpx/urllib3 log per-request INFO lines that duplicate the access
    # log; keep them quiet unless explicitly enabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True


def set_request_id(request_id: str) -> contextvars.Token:
    """Set the request id; returns a token for a scope-safe reset."""
    return request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token | None = None) -> None:
    """Reset to the previous value (token) or to the default."""
    if token is not None:
        request_id_var.reset(token)
    else:
        request_id_var.set("-")
