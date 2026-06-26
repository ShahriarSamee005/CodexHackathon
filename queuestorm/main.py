"""FastAPI application entry point: app instance, route definitions, and error handlers.

Design notes:
- Complaint text is untrusted; transaction history is the source of truth.
- Every error response uses the ErrorResponse envelope and NEVER includes stack
  traces, internal exception text, file paths, or secrets/API keys.
- Validation status mapping (observable contract):
    * Structural problems  (missing fields, wrong types)        -> 400
    * Semantic problems    (empty complaint / business rules)   -> 422
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:  # package-relative imports (e.g. `uvicorn queuestorm.main:app` from repo root)
    from .analyzer import analyze
    from .models import ErrorResponse, TicketRequest, TicketResponse
except ImportError:  # pragma: no cover - flat-script imports (e.g. `python main.py`)
    from analyzer import analyze
    from models import ErrorResponse, TicketRequest, TicketResponse

load_dotenv()

logger = logging.getLogger("queuestorm")

app = FastAPI(
    title="QueueStorm Investigator",
    version="1.0.0",
    description=(
        "Fintech support copilot that investigates tickets by cross-referencing "
        "the (untrusted) complaint against the (source-of-truth) transaction history."
    ),
)

# Judges may call from any origin. No credentials are used, so a wildcard origin
# is safe (and required: credentials + wildcard is rejected by browsers).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Routes — exactly two. (/docs and /openapi.json are framework docs, not API.) #
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> dict:
    """Liveness probe. No auth, no headers; trivial so cold start stays well under 60s."""
    return {"status": "ok"}


@app.post("/analyze-ticket", response_model=TicketResponse)
async def analyze_ticket(ticket: TicketRequest) -> TicketResponse:
    """Investigate a support ticket and return a structured verdict.

    Declared async: analyze() awaits the LLM text-generation step (with a hard
    10s timeout and an always-safe fallback), so it must run on the event loop.
    """
    return await analyze(ticket)


# --------------------------------------------------------------------------- #
# Error handlers — the response body is always a safe ErrorResponse envelope.   #
# --------------------------------------------------------------------------- #
def _field_names(errors) -> list:
    """Collect offending field names (safe to expose) from validation errors."""
    names = []
    for err in errors:
        # Skip "body" and integer loc parts (list indices / JSON decode positions);
        # keep only real field names so a malformed body doesn't surface a stray "0".
        parts = [str(p) for p in err.get("loc", ()) if p != "body" and not isinstance(p, int)]
        name = ".".join(parts)
        if name and name not in names:
            names.append(name)
    return names


def _semantic_detail(errors) -> str:
    """Build a safe detail string from our own (controlled) validator messages."""
    messages = []
    for err in errors:
        msg = err.get("msg", "")
        prefix = "Value error, "  # Pydantic prepends this to ValueError text
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
        if msg and msg not in messages:
            messages.append(msg)
    return "; ".join(messages) if messages else "Request failed validation."


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    """Body validation failures.

    Pydantic surfaces ValueErrors raised inside model validators (e.g. the empty
    complaint check) as `value_error` entries here — those are semantic -> 422.
    Missing fields and type/enum mismatches are structural -> 400.
    """
    errors = exc.errors()
    if errors and all(err.get("type") == "value_error" for err in errors):
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="unprocessable_entity",
                detail=_semantic_detail(errors),
            ).model_dump(),
        )

    fields = _field_names(errors)
    detail = (
        "Missing or invalid fields: " + ", ".join(fields)
        if fields
        else "Request body is malformed or has invalid types."
    )
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(error="validation_error", detail=detail).model_dump(),
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Semantic errors raised by our own business logic during a request.

    The raw exception text is NEVER returned to the client (it could carry
    internal detail); it is logged server-side only, and the caller gets a fixed,
    safe message. Pydantic validator ValueErrors take the RequestValidationError
    path above, not this one.
    """
    logger.warning("ValueError processing %s %s", request.method, request.url.path, exc_info=True)
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="unprocessable_entity",
            detail="The request could not be processed.",
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all. Logs server-side for debugging; the client gets a generic,
    leak-free message (no stack trace, no exception text, no paths, no secrets)."""
    logger.exception("Unhandled error processing %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="internal_error",
            detail="An internal error occurred while processing the request.",
        ).model_dump(),
    )


# NOTE: HTTPException is intentionally left unhandled here so it passes through to
# FastAPI's default handler unchanged (per the contract).


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
