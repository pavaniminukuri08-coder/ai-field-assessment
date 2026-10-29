
import json
import os
import re
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


app = FastAPI(title="Streaming LLM Gateway")

REDACTED = "[REDACTED]"

EMAIL_PATTERN = (
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    r"(?![\w.+-])"
)

SSN_PATTERN = r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"

# Require the number not to continue into another digit, space, or hyphen.
CARD_PATTERN = r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"

PII_PATTERN = re.compile(
    f"{EMAIL_PATTERN}|{SSN_PATTERN}|{CARD_PATTERN}"
)

EMAIL_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    ".!#$%&'*+/=?^_`{|}~-@"
)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)


class PIIRedactor:
    """Redact PII while retaining unfinished values between chunks."""

    def __init__(self):
        self.buffer = ""

    @staticmethod
    def redact(text: str) -> str:
        return PII_PATTERN.sub(REDACTED, text)

    def _safe_cut(self) -> int:
        text = self.buffer

        if not text:
            return 0

        candidates = []

        # Hold an email-like suffix until a delimiter arrives.
        if text[-1] in EMAIL_CHARS:
            start = len(text) - 1

            while start > 0 and text[start - 1] in EMAIL_CHARS:
                start -= 1

            candidates.append(start)

        # Hold a possible numeric value, including trailing spaces
        # or hyphens that may separate digits across chunks.
        numeric_end = len(text)

        while (
            numeric_end > 0
            and text[numeric_end - 1] in " \t\r\n-.,;:!?)]}\"'"
        ):
            numeric_end -= 1

        if numeric_end > 0 and text[numeric_end - 1].isdigit():
            start = numeric_end - 1

            while start > 0 and (
                text[start - 1].isdigit()
                or text[start - 1] in " -"
            ):
                start -= 1

            candidates.append(start)

        if not candidates:
            return len(text)

        return min(candidates)

    def feed(self, chunk: str, final: bool = False) -> str:
        self.buffer += chunk

        if final:
            result = self.redact(self.buffer)
            self.buffer = ""
            return result

        cut = self._safe_cut()

        if cut == 0:
            return ""

        result = self.redact(self.buffer[:cut])
        self.buffer = self.buffer[cut:]

        return result


async def stream_llm_deltas(prompt: str) -> AsyncIterator[str]:
    """Stream text deltas from an OpenAI-compatible LLM provider."""
    api_key = os.getenv("LLM_API_KEY")

    if not api_key:
        raise RuntimeError("LLM_API_KEY is not configured")

    url = os.getenv(
        "LLM_CHAT_COMPLETIONS_URL",
        "https://api.openai.com/v1/chat/completions",
    )
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }

    timeout = httpx.Timeout(
        connect=10.0,
        read=None,
        write=10.0,
        pool=10.0,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue

                data = line[len("data:"):].strip()

                if data == "[DONE]":
                    break

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = event.get("choices", [])

                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                content = delta.get("content")

                if isinstance(content, str) and content:
                    yield content


async def redacted_stream(prompt: str) -> AsyncIterator[str]:
    """Redact generated content before returning each SSE event."""
    redactor = PIIRedactor()

    try:
        async for delta in stream_llm_deltas(prompt):
            safe_text = redactor.feed(delta)

            if safe_text:
                yield f"data: {json.dumps({'text': safe_text})}\n\n"

        final_text = redactor.feed("", final=True)

        if final_text:
            yield f"data: {json.dumps({'text': final_text})}\n\n"

        yield "data: [DONE]\n\n"

    except Exception:
        yield (
            "event: error\n"
            'data: {"error":"Upstream generation failed"}\n\n'
        )


@app.post("/generate")
async def generate(request: GenerateRequest):
    if not os.getenv("LLM_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="LLM provider is not configured",
        )

    return StreamingResponse(
        redacted_stream(request.prompt),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
