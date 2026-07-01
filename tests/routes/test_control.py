"""control 라우터 pytest — /control/assign-group 9건 (Phase 17.5)"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from server.config import settings
from server.routes import control

TEST_SECRET = "test-engine-secret"
TEST_GROUP_ID = "507f1f77bcf86cd799439011"
ANOTHER_GROUP_ID = "aaaaaaaaaaaaaaaaaaaaaaaa"
BACKEND_DUAL_URL = f"{settings.backend_url}/api/engine/register-dual"


def _build_pending_app():
    """pending 상태 DE app 생성함 (lifespan 없이 state만 초기화)"""
    app = FastAPI()
    app.include_router(control.router)
    app.state.public_url = "http://testhost:5002"
    app.state.subject_index = 1
    app.state.secret_key = TEST_SECRET
    app.state.registered_group_id = None
    app.state.heartbeat_task = None
    app.state.assign_lock = asyncio.Lock()
    return app


@pytest.fixture
def pending_app():
    """pending app fixture — teardown 시 heartbeat_task 정리함"""
    app = _build_pending_app()
    yield app
    # teardown: heartbeat_task cancel 수행함
    if app.state.heartbeat_task is not None:
        app.state.heartbeat_task.cancel()


def _assign(client: TestClient, group_id: str, secret: str = TEST_SECRET):
    """공통 assign-group 요청 헬퍼 반환함"""
    return client.post(
        "/control/assign-group",
        json={"group_id": group_id},
        headers={"X-Engine-Secret": secret},
    )


def test_assign_group_new_registration_returns_200(
    pending_app: FastAPI, httpx_mock: HTTPXMock
):
    """pending 상태에서 신규 assign → 200 registered + state 전이 확인함"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    client = TestClient(pending_app)

    resp = _assign(client, TEST_GROUP_ID)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "registered"
    assert body["groupId"] == TEST_GROUP_ID
    assert pending_app.state.registered_group_id == TEST_GROUP_ID
    assert pending_app.state.heartbeat_task is not None


def test_assign_group_idempotent_same_group_returns_200(
    pending_app: FastAPI, httpx_mock: HTTPXMock
):
    """이미 X로 등록된 상태에서 X 재호출 → 200 already_registered 확인함"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    client = TestClient(pending_app)

    first = _assign(client, TEST_GROUP_ID)
    assert first.status_code == 200

    second = _assign(client, TEST_GROUP_ID)
    assert second.status_code == 200
    assert second.json()["status"] == "already_registered"
    assert second.json()["groupId"] == TEST_GROUP_ID


def test_assign_group_idempotent_no_backend_re_call(
    pending_app: FastAPI, httpx_mock: HTTPXMock
):
    """same group_id 재호출 시 register_to_backend_dual 호출 카운트 1 유지 확인함"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    client = TestClient(pending_app)

    _assign(client, TEST_GROUP_ID)
    _assign(client, TEST_GROUP_ID)
    _assign(client, TEST_GROUP_ID)

    # httpx_mock은 등록된 요청 순서로 매칭함. 총 호출 수가 1이어야 멱등 성립함
    requests = httpx_mock.get_requests(url=BACKEND_DUAL_URL)
    assert len(requests) == 1


def test_assign_group_idempotent_no_heartbeat_duplicate(
    pending_app: FastAPI, httpx_mock: HTTPXMock
):
    """same group_id 재호출 시 heartbeat_task 동일 객체(재생성 안 됨) 확인함"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    client = TestClient(pending_app)

    _assign(client, TEST_GROUP_ID)
    task_first = pending_app.state.heartbeat_task
    assert task_first is not None

    _assign(client, TEST_GROUP_ID)
    task_second = pending_app.state.heartbeat_task

    # 재호출이 heartbeat 재생성을 하지 않아야 함 — 동일 Task 객체여야 함
    assert task_second is task_first


def test_assign_group_concurrent_requests_serialized(
    pending_app: FastAPI, httpx_mock: HTTPXMock
):
    """동시 2건 요청 → Lock 직렬화 → 하나 registered, 다른 하나 already_registered 확인함"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)

    # TestClient는 동기 wrapper라 asyncio.gather로 httpx.AsyncClient 사용함
    from httpx import ASGITransport, AsyncClient

    async def _concurrent():
        transport = ASGITransport(app=pending_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"group_id": TEST_GROUP_ID}
            headers = {"X-Engine-Secret": TEST_SECRET}
            return await asyncio.gather(
                ac.post("/control/assign-group", json=payload, headers=headers),
                ac.post("/control/assign-group", json=payload, headers=headers),
            )

    results = asyncio.get_event_loop().run_until_complete(_concurrent())

    statuses = sorted([r.json()["status"] for r in results])
    assert statuses == ["already_registered", "registered"]
    # backend 호출은 정확히 1회만 발생해야 함 (Lock 직렬화 증거)
    requests = httpx_mock.get_requests(url=BACKEND_DUAL_URL)
    assert len(requests) == 1


def test_assign_group_different_group_idle_rebinds_200(
    pending_app: FastAPI, httpx_mock: HTTPXMock
):
    """X 등록됨(측정중 아님) + Y 호출 → idle-rebind 200 + supersede 확인함 (Phase 19)"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    client = TestClient(pending_app)

    _assign(client, TEST_GROUP_ID)
    resp = _assign(client, ANOTHER_GROUP_ID)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "registered"
    assert body["groupId"] == ANOTHER_GROUP_ID
    assert body["superseded"] == TEST_GROUP_ID
    assert pending_app.state.registered_group_id == ANOTHER_GROUP_ID


def test_assign_group_different_group_measuring_returns_409(
    pending_app: FastAPI, httpx_mock: HTTPXMock, monkeypatch
):
    """X 등록됨 + 측정중 + Y 호출 → 409 group_id_conflict(라이브 보호) 확인함 (Phase 19)"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    client = TestClient(pending_app)

    _assign(client, TEST_GROUP_ID)

    # TEST_GROUP_ID로 실행 중인 스트림 존재하도록 mock함 (측정중 판정)
    monkeypatch.setattr(
        control,
        "get_all_status",
        lambda: [{"key": f"{TEST_GROUP_ID}:1", "pid": 1, "running": True}],
    )
    resp = _assign(client, ANOTHER_GROUP_ID)

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error"] == "group_id_conflict"
    assert body["detail"]["current"] == TEST_GROUP_ID
    assert body["detail"]["reason"] == "measuring"


def test_release_clears_binding(pending_app: FastAPI, httpx_mock: HTTPXMock):
    """register 후 release → 바인딩 해제 + released=true 확인함 (Phase 19)"""
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    client = TestClient(pending_app)

    _assign(client, TEST_GROUP_ID)
    resp = client.post(
        "/control/release", json={}, headers={"X-Engine-Secret": TEST_SECRET}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["released"] is True
    assert body["previous"] == TEST_GROUP_ID
    assert pending_app.state.registered_group_id is None


def test_release_bad_secret_returns_401(pending_app: FastAPI):
    """release secret mismatch → 401 확인함 (Phase 19)"""
    client = TestClient(pending_app)
    resp = client.post(
        "/control/release", json={}, headers={"X-Engine-Secret": "wrong"}
    )
    assert resp.status_code == 401


def test_assign_group_invalid_body_returns_422(pending_app: FastAPI):
    """잘못된 body → FastAPI validation 422 확인함 (pydantic 기본)"""
    client = TestClient(pending_app)
    resp = client.post(
        "/control/assign-group",
        json={},
        headers={"X-Engine-Secret": TEST_SECRET},
    )
    # FastAPI/pydantic 기본 unprocessable entity
    assert resp.status_code == 422


def test_assign_group_invalid_secret_returns_401(pending_app: FastAPI):
    """헤더 secret mismatch → 401 invalid_secret 확인함"""
    client = TestClient(pending_app)
    resp = _assign(client, TEST_GROUP_ID, secret="wrong-secret")

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_secret"


def test_assign_group_backend_failure_returns_502(
    pending_app: FastAPI, httpx_mock: HTTPXMock
):
    """BE /register-dual 실패 → 502 + state pending 유지 + 재시도 가능 확인함"""
    # 첫 호출은 실패함
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=500)

    client = TestClient(pending_app)
    resp = _assign(client, TEST_GROUP_ID)

    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "backend_register_failed"
    # state는 pending 유지 — 재시도 가능함
    assert pending_app.state.registered_group_id is None
    assert pending_app.state.heartbeat_task is None

    # 두번째 호출은 성공 → 정상 register 가능 확인함
    httpx_mock.add_response(url=BACKEND_DUAL_URL, method="POST", status_code=200)
    resp2 = _assign(client, TEST_GROUP_ID)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "registered"
    assert pending_app.state.registered_group_id == TEST_GROUP_ID
