"""
PR-B1: Auth hardening and SSE token redesign.

Tests cover:
- POST /tasks/{id}/stream-token: requires Bearer auth, returns scoped token.
- GET /tasks/{id}/stream: accepts short-lived stream token via ?token=.
- Stream token expiry (expired token → 401).
- Stream token scoped to task (token for task A rejected on task B).
- Stream token rejected on non-stream endpoints.
- Empty API_KEY without INSECURE_LOCAL_AUTH now returns 401 (fail-closed).
- INSECURE_LOCAL_AUTH=1 allows unauthenticated access.
"""
import time
import pytest
from httpx import AsyncClient, ASGITransport


_API_KEY = "test-stream-tok-key"


@pytest.fixture
async def auth_client(monkeypatch):
    """Client with API_KEY set, lifespan bypassed (DB init'd by isolated_db)."""
    from backend import config
    from backend.main import app
    monkeypatch.setattr(config.settings, "api_key", _API_KEY)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# POST /tasks/{task_id}/stream-token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_token_endpoint_requires_bearer(auth_client):
    """Calling stream-token endpoint without credentials returns 401."""
    from backend import db
    task = await db.create_task("tok-test", "prompt")
    r = await auth_client.post(f"/tasks/{task['id']}/stream-token")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_token_endpoint_rejects_query_param_key(auth_client):
    """?token= master-key auth is NOT accepted on the stream-token endpoint."""
    from backend import db
    task = await db.create_task("tok-query", "prompt")
    r = await auth_client.post(f"/tasks/{task['id']}/stream-token?token={_API_KEY}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_token_endpoint_returns_token(auth_client):
    """Valid Bearer auth → returns token dict with expected fields."""
    from backend import db
    task = await db.create_task("tok-return", "prompt")
    r = await auth_client.post(
        f"/tasks/{task['id']}/stream-token",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert data["task_id"] == task["id"]
    assert isinstance(data["expires_in"], int)
    assert data["expires_in"] > 0


@pytest.mark.asyncio
async def test_stream_token_endpoint_404_for_unknown_task(auth_client):
    """Stream-token endpoint returns 404 when the task does not exist."""
    r = await auth_client.post(
        "/tasks/nonexistent-id/stream-token",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}/stream — stream token acceptance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_accepts_valid_stream_token(auth_client):
    """SSE endpoint accepts a freshly-issued stream token via ?token=."""
    from backend import db
    from backend.main import _generate_stream_token
    from unittest.mock import AsyncMock, patch
    from starlette.requests import Request

    task = await db.create_task("sse-tok-ok", "prompt")
    token = _generate_stream_token(task["id"])

    with patch.object(Request, "is_disconnected", AsyncMock(return_value=True)):
        r = await auth_client.get(f"/tasks/{task['id']}/stream?token={token}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_stream_rejects_expired_token(auth_client, monkeypatch):
    """Expired stream token returns 401."""
    from backend import db
    from backend import main as _main
    from backend import config

    task = await db.create_task("sse-tok-expired", "prompt")

    # Generate a token whose expiry is already in the past.
    original_time = time.time
    monkeypatch.setattr(config.settings, "stream_token_ttl_seconds", -1)
    monkeypatch.setattr(_main, "_generate_stream_token",
                        lambda tid: _main._generate_stream_token.__wrapped__(tid)
                        if hasattr(_main._generate_stream_token, "__wrapped__")
                        else _expired_token(tid, _API_KEY))

    expired_token = _expired_token(task["id"], _API_KEY)
    r = await auth_client.get(f"/tasks/{task['id']}/stream?token={expired_token}")
    assert r.status_code == 401


def _expired_token(task_id: str, api_key: str) -> str:
    """Helper: build an already-expired HMAC token for testing."""
    import hashlib
    import hmac as _hmac_mod
    expiry = int(time.time()) - 1  # expired 1 second ago
    payload = f"{task_id}.{expiry}"
    sig = _hmac_mod.new(api_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


@pytest.mark.asyncio
async def test_stream_rejects_token_for_wrong_task(auth_client):
    """A stream token issued for task A is rejected on task B's stream."""
    from backend import db
    from backend.main import _generate_stream_token

    task_a = await db.create_task("sse-tok-a", "prompt")
    task_b = await db.create_task("sse-tok-b", "prompt")
    token_a = _generate_stream_token(task_a["id"])

    r = await auth_client.get(f"/tasks/{task_b['id']}/stream?token={token_a}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_rejects_master_key_as_query_token(auth_client):
    """Master API key passed as ?token= is no longer accepted on stream endpoint."""
    from backend import db
    task = await db.create_task("sse-master-tok", "prompt")
    r = await auth_client.get(f"/tasks/{task['id']}/stream?token={_API_KEY}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_accepts_bearer_header(auth_client):
    """Stream endpoint still accepts Authorization: Bearer <master-key>."""
    r = await auth_client.get(
        "/tasks/nonexistent/stream",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )
    # 404 because task doesn't exist, but auth passed → NOT 401
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Route scoping: stream token ONLY works on the stream endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_token_rejected_on_list_tasks(auth_client):
    """A stream token must not authorize the GET /tasks list endpoint."""
    from backend import db
    from backend.main import _generate_stream_token
    task = await db.create_task("scope-test", "prompt")
    token = _generate_stream_token(task["id"])

    r = await auth_client.get(f"/tasks?token={token}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_token_rejected_on_get_task(auth_client):
    """A stream token must not authorize the GET /tasks/{id} endpoint."""
    from backend import db
    from backend.main import _generate_stream_token
    task = await db.create_task("scope-get-test", "prompt")
    token = _generate_stream_token(task["id"])

    r = await auth_client.get(f"/tasks/{task['id']}?token={token}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_token_rejected_on_create_task(auth_client):
    """A stream token must not authorize POST /tasks."""
    from backend import db
    from backend.main import _generate_stream_token
    task = await db.create_task("scope-post-test", "prompt")
    token = _generate_stream_token(task["id"])

    r = await auth_client.post(
        f"/tasks?token={token}",
        json={"title": "t", "prompt": "p"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Empty API_KEY fail-closed behaviour (PR-B1 acceptance criterion 3)
# ---------------------------------------------------------------------------

@pytest.fixture
async def no_auth_client(monkeypatch):
    """Client with API_KEY empty and INSECURE_LOCAL_AUTH=False (default)."""
    from backend import config
    from backend.main import app
    monkeypatch.setattr(config.settings, "api_key", "")
    monkeypatch.setattr(config.settings, "insecure_local_auth", False)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.fixture
async def insecure_local_client(monkeypatch):
    """Client with API_KEY empty and INSECURE_LOCAL_AUTH=True (explicit dev mode)."""
    from backend import config
    from backend.main import app
    monkeypatch.setattr(config.settings, "api_key", "")
    monkeypatch.setattr(config.settings, "insecure_local_auth", True)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_empty_api_key_fails_closed_on_list_tasks(no_auth_client):
    """Empty API_KEY without INSECURE_LOCAL_AUTH → protected endpoints return 401."""
    r = await no_auth_client.get("/tasks")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_empty_api_key_fails_closed_on_stream(no_auth_client):
    """Empty API_KEY without INSECURE_LOCAL_AUTH → stream endpoint returns 401."""
    r = await no_auth_client.get("/tasks/some-id/stream")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_empty_api_key_fails_closed_on_create_task(no_auth_client):
    """Empty API_KEY without INSECURE_LOCAL_AUTH → POST /tasks returns 401."""
    r = await no_auth_client.post("/tasks", json={"title": "t", "prompt": "p"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_insecure_local_auth_allows_list_tasks(insecure_local_client):
    """INSECURE_LOCAL_AUTH=1 with empty API_KEY → all endpoints are open."""
    r = await insecure_local_client.get("/tasks")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_health_always_open(no_auth_client):
    """/health is always unauthenticated regardless of API_KEY settings."""
    r = await no_auth_client.get("/health")
    assert r.status_code == 200
