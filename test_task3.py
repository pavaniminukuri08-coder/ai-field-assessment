
import pytest
from fastapi.testclient import TestClient

import task3_gateway
from task3_gateway import PIIRedactor, app


def redact_chunks(chunks):
    """Feed chunks incrementally and collect the final safe output."""
    redactor = PIIRedactor()
    output = []

    for chunk in chunks:
        output.append(redactor.feed(chunk))

    output.append(redactor.feed("", final=True))
    return "".join(output)


def test_email_split_across_chunks():
    output = redact_chunks([
        "Please contact ",
        "pavani@",
        "example.com ",
        "for details.",
    ])

    assert "pavani@example.com" not in output
    assert "[REDACTED]" in output
    assert "Please contact " in output


def test_ssn_split_across_chunks():
    output = redact_chunks([
        "Your SSN is 123-",
        "45-",
        "6789. Thank you.",
    ])

    assert "123-45-6789" not in output
    assert "[REDACTED]" in output
    assert "Thank you." in output


def test_credit_card_split_across_chunks():
    output = redact_chunks([
        "Card number: 4111 1111 ",
        "1111 1111 ",
        "was provided.",
    ])

    assert "4111 1111 1111 1111" not in output
    assert "[REDACTED]" in output
    assert "was provided." in output


def test_multiple_pii_values_in_one_chunk():
    output = redact_chunks([
        "Email: pavani@example.com, "
        "SSN: 123-45-6789, "
        "card: 4111 1111 1111 1111."
    ])

    assert "pavani@example.com" not in output
    assert "123-45-6789" not in output
    assert "4111 1111 1111 1111" not in output
    assert output.count("[REDACTED]") == 3


def test_safe_text_is_emitted_incrementally():
    redactor = PIIRedactor()

    first_output = redactor.feed("Hello there. ")

    assert "Hello there." in first_output


def test_http_stream_redacts_pii(monkeypatch):
    async def fake_stream(prompt):
        chunks = [
            "Contact ",
            "pavani@",
            "example.com ",
            "for assistance.",
        ]

        for chunk in chunks:
            yield chunk

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        task3_gateway,
        "stream_llm_deltas",
        fake_stream,
    )

    client = TestClient(app)

    response = client.post(
        "/generate",
        json={"prompt": "Write a short response."},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "text/event-stream"
    )

    assert "pavani@example.com" not in response.text
    assert "[REDACTED]" in response.text
    assert "[DONE]" in response.text


def test_missing_api_key_returns_service_unavailable(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    client = TestClient(app)

    response = client.post(
        "/generate",
        json={"prompt": "Hello"},
    )

    assert response.status_code == 503
