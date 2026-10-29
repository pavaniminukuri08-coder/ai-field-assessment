import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import task4_router as router


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Use a temporary database so tests don't affect your real rate limits.
    monkeypatch.setattr(
        router,
        "limiter",
        router.SQLiteTokenLimiter(
            str(tmp_path / "test_rates.sqlite3"),
            limit=5000,
        ),
    )

    monkeypatch.setenv("PRIMARY_LLM_API_KEY", "test-primary-key")
    monkeypatch.setenv("BACKUP_LLM_API_KEY", "test-backup-key")
    monkeypatch.setenv("BACKUP_LLM_URL", "https://backup.example.com/chat")

    return TestClient(router.app)


def fake_response(status_code, body=None):
    return httpx.Response(
        status_code,
        json=body or {},
        request=httpx.Request("POST", "https://example.com"),
    )


def test_missing_api_key(client):
    response = client.post(
        "/v1/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_primary_success(client, monkeypatch):
    async def fake_call(provider, request):
        return fake_response(
            200,
            {
                "choices": [{"message": {"content": "Hello!"}}],
                "usage": {"total_tokens": 10},
            },
        )

    monkeypatch.setattr(router, "call_provider", fake_call)

    response = client.post(
        "/v1/completions",
        headers={"Authorization": "Bearer tenant-1"},
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Hello!"


def test_primary_429_uses_backup(client, monkeypatch):
    calls = []

    async def fake_call(provider, request):
        calls.append(provider)

        if provider == "primary":
            return fake_response(429)

        return fake_response(
            200,
            {
                "choices": [{"message": {"content": "Backup response"}}],
                "usage": {"total_tokens": 12},
            },
        )

    monkeypatch.setattr(router, "call_provider", fake_call)

    response = client.post(
        "/v1/completions",
        headers={"Authorization": "Bearer tenant-2"},
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    assert calls == ["primary", "backup"]


def test_primary_timeout_uses_backup(client, monkeypatch):
    calls = []

    async def fake_call(provider, request):
        calls.append(provider)

        if provider == "primary":
            raise httpx.TimeoutException("internal timeout details")

        return fake_response(
            200,
            {
                "choices": [{"message": {"content": "Backup response"}}],
                "usage": {"total_tokens": 12},
            },
        )

    monkeypatch.setattr(router, "call_provider", fake_call)

    response = client.post(
        "/v1/completions",
        headers={"Authorization": "Bearer tenant-3"},
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    assert calls == ["primary", "backup"]


def test_rate_limit_rejects_excess_tokens(tmp_path):
    limiter = router.SQLiteTokenLimiter(
        str(tmp_path / "limited.sqlite3"),
        limit=10,
    )

    async def run_test():
        first = await limiter.reserve("tenant", 8)
        second = await limiter.reserve("tenant", 5)

        assert first is not None
        assert second is None

    asyncio.run(run_test())


def test_upstream_error_does_not_leak_details(client, monkeypatch):
    async def fake_call(provider, request):
        return fake_response(500, {"detail": "secret internal traceback"})

    monkeypatch.setattr(router, "call_provider", fake_call)

    response = client.post(
        "/v1/completions",
        headers={"Authorization": "Bearer tenant-4"},
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 502
    assert "secret internal traceback" not in response.text
