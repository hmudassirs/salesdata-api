"""Request observability (logging/tracing/audit) middleware.

Extracted from core/app/api/app.py's single `log_requests` middleware —
see core/auth/middleware.py for the half that decided whether the
request was allowed through at all. This half only records what
happened; it must never be able to change the response it's recording.
"""

import asyncio
import time

from fastapi import FastAPI, Request, Response

from core.db.logger import get_logger
from core.observability.context import emit_request_observability

logger = get_logger(__name__)


def _record(app: FastAPI, request: Request, **kwargs) -> None:
    """Call emit_request_observability, dispatching to a worker thread
    only when it would actually do blocking I/O.

    With the background write queue active (the normal case —
    see ApplicationLifespan's ServiceDatabaseStep), emit_request_
    observability() just builds a dict and does an in-memory queue put;
    there's no I/O to offload, and routing it through asyncio.to_thread
    anyway means competing for the same thread pool every real DB call
    also needs, for no benefit. Only the no-queue fallback path (rare —
    simplified test setups) does synchronous SQLite I/O and genuinely
    needs the thread.
    """
    service_manager = app.state.service_manager
    if getattr(service_manager, "observability_queue", None) is not None:
        emit_request_observability(service_manager, request, **kwargs)
        return None  # caller doesn't need to await anything
    return asyncio.to_thread(
        emit_request_observability, service_manager, request, **kwargs
    )


def install_observability_middleware(app: FastAPI) -> None:
    """Register the observability middleware on `app`."""

    @app.middleware("http")
    async def record_observability(request: Request, call_next):
        """Log/trace/audit every request. Best-effort: a failure here
        must never turn an otherwise-successful response into a 500 —
        it used to, since this call wasn't guarded and its exception
        propagated straight out of the middleware."""
        start_time = time.time()

        try:
            response: Response = await call_next(request)
            process_time = time.time() - start_time

            if hasattr(app.state, "service_manager"):
                try:
                    maybe_awaitable = _record(
                        app,
                        request,
                        status_code=response.status_code,
                        duration_ms=process_time * 1000,
                        user_id=getattr(request.state, "user_id", None),
                    )
                    if maybe_awaitable is not None:
                        await maybe_awaitable
                except Exception:
                    logger.warning(
                        "Failed to record request observability", exc_info=True
                    )

            return response

        except Exception as e:
            process_time = time.time() - start_time

            if hasattr(app.state, "service_manager"):
                try:
                    maybe_awaitable = _record(
                        app,
                        request,
                        status_code=500,
                        duration_ms=process_time * 1000,
                        user_id=getattr(request.state, "user_id", None),
                        error=str(e),
                    )
                    if maybe_awaitable is not None:
                        await maybe_awaitable
                except Exception:
                    logger.warning(
                        "Failed to record request observability", exc_info=True
                    )

            raise
