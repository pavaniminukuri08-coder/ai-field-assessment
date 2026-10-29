
import asyncio
import hashlib
import json
import os
import sqlite3
import time
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


load_dotenv()

DB_PATH = os.getenv("RATE_LIMIT_DB_PATH", "rate_limits.sqlite3")
TOKEN_LIMIT = 50_000
WINDOW_SECONDS = 60
PRIMARY_TIMEOUT_SECONDS = 3.0
BACKUP_TIMEOUT_SECONDS = 10.0

app = FastAPI(title="LLM Model Router")


class CompletionRequest(BaseModel):
    messages: list[dict] = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=16000)
    temperature: float = Field(default=0.7, ge=0, le=2)


class GatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        error_type: str,
        code: str,
        message: str,
    ):
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.message = message


def error_response(error: GatewayError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "type": error.error_type,
                "code": error.code,
                "message": error.message,
            }
        },
    )


@app.exception_handler(GatewayError)
async def handle_gateway_error(request, exc: GatewayError):
    return error_response(exc)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "type": "validation_error",
                "code": "invalid_request",
                "message": "The request contains invalid or missing fields.",
            }
        },
    )


def tenant_id_from_key(api_key: str) -> str:
    """Use a hash as the tenant identifier instead of storing the raw key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Estimate tokens if the provider does not return usage information."""
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except (ImportError, ValueError, KeyError):
        return max(1, (len(text) + 3) // 4)


def estimate_prompt_tokens(messages: list[dict]) -> int:
    text = "\n".join(
        str(message.get("content", ""))
        for message in messages
    )
    return estimate_tokens(text)


class SQLiteTokenLimiter:
    """Track token reservations in a rolling 60-second window."""

    def __init__(self, db_path: str, limit: int = TOKEN_LIMIT):
        self.db_path = db_path
        self.limit = limit
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS token_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    tokens INTEGER NOT NULL CHECK(tokens >= 0)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_token_events_tenant_time
                ON token_events (tenant_id, created_at)
                """
            )

    def _reserve_sync(self, tenant_id: str, tokens: int) -> int | None:
        now = time.time()
        cutoff = now - WINDOW_SECONDS

        with self._connect() as connection:
            # Serialize usage checks and reservations across connections.
            connection.execute("BEGIN IMMEDIATE")

            # Evict expired entries from the sliding window.
            connection.execute(
                "DELETE FROM token_events WHERE created_at <= ?",
                (cutoff,),
            )

            row = connection.execute(
                """
                SELECT COALESCE(SUM(tokens), 0)
                FROM token_events
                WHERE tenant_id = ? AND created_at > ?
                """,
                (tenant_id, cutoff),
            ).fetchone()

            current_usage = row[0]

            if tokens <= 0 or tokens > self.limit:
                connection.rollback()
                return None

            if current_usage + tokens > self.limit:
                connection.rollback()
                return None

            cursor = connection.execute(
                """
                INSERT INTO token_events (tenant_id, created_at, tokens)
                VALUES (?, ?, ?)
                """,
                (tenant_id, now, tokens),
            )

            return cursor.lastrowid

    async def reserve(self, tenant_id: str, tokens: int) -> int | None:
        return await asyncio.to_thread(
            self._reserve_sync,
            tenant_id,
            tokens,
        )

    def _finish_sync(
        self,
        event_id: int,
        actual_tokens: int | None,
    ):
        with self._connect() as connection:
            if actual_tokens is None:
                connection.execute(
                    "DELETE FROM token_events WHERE id = ?",
                    (event_id,),
                )
            else:
                connection.execute(
                    "UPDATE token_events SET tokens = ? WHERE id = ?",
                    (max(0, actual_tokens), event_id),
                )

    async def finish(self, event_id: int, actual_tokens: int | None):
        await asyncio.to_thread(
            self._finish_sync,
            event_id,
            actual_tokens,
        )


limiter = SQLiteTokenLimiter(DB_PATH)


def provider_settings(provider: Literal["primary", "backup"]):
    if provider == "primary":
        return (
            os.getenv(
                "PRIMARY_LLM_URL",
                "https://api.openai.com/v1/chat/completions",
            ),
            os.getenv("PRIMARY_LLM_API_KEY"),
            os.getenv("PRIMARY_LLM_MODEL", "gpt-4o-mini"),
        )

    return (
        os.getenv("BACKUP_LLM_URL"),
        os.getenv("BACKUP_LLM_API_KEY"),
        os.getenv("BACKUP_LLM_MODEL"),
    )


async def call_provider(
    provider: Literal["primary", "backup"],
    request: CompletionRequest,
) -> httpx.Response:
    url, api_key, model = provider_settings(provider)

    if not url or not api_key or not model:
        raise GatewayError(
            503,
            "configuration_error",
            "provider_not_configured",
            "The requested model provider is not configured.",
        )

    payload = {
        "model": model,
        "messages": request.messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }

    timeout_seconds = (
        PRIMARY_TIMEOUT_SECONDS
        if provider == "primary"
        else BACKUP_TIMEOUT_SECONDS
    )

    timeout = httpx.Timeout(timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )


async def route_completion(request: CompletionRequest) -> httpx.Response:
    # Fail over only for primary HTTP 429 or timeout.
    try:
        primary_response = await call_provider("primary", request)

        if primary_response.status_code == 429:
            pass
        elif primary_response.is_success:
            return primary_response
        else:
            raise GatewayError(
                502,
                "upstream_error",
                "primary_provider_error",
                "The primary model provider could not complete the request.",
            )

    except httpx.TimeoutException:
        pass
    except GatewayError:
        raise
    except httpx.RequestError:
        raise GatewayError(
            502,
            "upstream_error",
            "primary_connection_error",
            "The primary model provider is unavailable.",
        ) from None

    try:
        backup_response = await call_provider("backup", request)

        if backup_response.is_success:
            return backup_response

        raise GatewayError(
            502,
            "upstream_error",
            "backup_provider_error",
            "The backup model provider could not complete the request.",
        )

    except httpx.TimeoutException:
        raise GatewayError(
            504,
            "timeout_error",
            "providers_timed_out",
            "The model providers did not respond in time.",
        ) from None
    except GatewayError:
        raise
    except httpx.RequestError:
        raise GatewayError(
            502,
            "upstream_error",
            "backup_connection_error",
            "The backup model provider is unavailable.",
        ) from None


@app.post("/v1/completions")
async def create_completion(
    request: CompletionRequest,
    authorization: str | None = Header(default=None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise GatewayError(
            401,
            "authentication_error",
            "missing_api_key",
            "A valid tenant API key is required.",
        )

    tenant_key = authorization.removeprefix("Bearer ").strip()

    if not tenant_key:
        raise GatewayError(
            401,
            "authentication_error",
            "missing_api_key",
            "A valid tenant API key is required.",
        )

    tenant_id = tenant_id_from_key(tenant_key)

    # Reserve input tokens plus the maximum requested output tokens.
    reserved_tokens = (
        estimate_prompt_tokens(request.messages) + request.max_tokens
    )

    event_id = await limiter.reserve(tenant_id, reserved_tokens)

    if event_id is None:
        raise GatewayError(
            429,
            "rate_limit_error",
            "token_limit_exceeded",
            "The tenant token limit has been reached. Try again later.",
        )

    try:
        response = await route_completion(request)
        result = response.json()

        usage = result.get("usage") or {}
        actual_tokens = usage.get("total_tokens")

        if not isinstance(actual_tokens, int) or actual_tokens < 0:
            output_text = json.dumps(result.get("choices", []))
            actual_tokens = (
                estimate_prompt_tokens(request.messages)
                + estimate_tokens(output_text)
            )

        # Never allow usage reconciliation to exceed the reservation.
        # The reservation includes the requested maximum output tokens.
        actual_tokens = min(actual_tokens, reserved_tokens)
        await limiter.finish(event_id, actual_tokens)

        return JSONResponse(
            status_code=response.status_code,
            content=result,
        )

    except GatewayError:
        await limiter.finish(event_id, None)
        raise
    except Exception:
        await limiter.finish(event_id, None)
        raise GatewayError(
            502,
            "gateway_error",
            "request_failed",
            "The gateway could not complete the request.",
        ) from None
