"""mock data-engine의 /control/assign-group 인증 계약 테스트.

실 DE(`server/routes/control.py:76`)는 X-Engine-Secret 헤더를 요구하는데
mock에만 빠져 있으면, mock에서 통과하는 호출을 실물이 거부하는 발산이 생긴다.
호출자(proxy `control-assign-group.ts`, Playwright e2e)는 실물 계약을 따라야 하므로
mock이 실물보다 느슨하면 안 된다.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

_MOCK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mock_data_engine.py"
DEFAULT_SECRET = "change-me-in-production"
GROUP_ID = "6a72db3c2d01f2ce5c690b19"


def _load_mock_module() -> Any:
    """scripts/mock_data_engine.py 를 패키지 없이 직접 로드함.

    scripts/ 는 패키지가 아니라 일반 import 가 불가함. 모듈이 import 시점에
    argparse 를 돌리지만 parse_known_args 라 pytest 인자에 영향받지 않음.
    """
    spec = importlib.util.spec_from_file_location("mock_data_engine", _MOCK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mock_data_engine"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mock_app() -> Any:
    return _load_mock_module()


def test_assign_group_without_header_is_rejected(mock_app) -> None:
    """헤더 없이 부르면 통과하지 못함 — 실물과 같은 계약임"""
    with TestClient(mock_app.app) as client:
        res = client.post("/control/assign-group", json={"group_id": GROUP_ID})

    assert res.status_code != 200
    # FastAPI 는 필수 헤더 누락을 422 로 거절함
    assert res.status_code == 422


def test_assign_group_with_wrong_secret_is_401(mock_app) -> None:
    """시크릿이 틀리면 실 DE control 라우트처럼 401과 invalid_secret을 반환함"""
    with TestClient(mock_app.app) as client:
        res = client.post(
            "/control/assign-group",
            json={"group_id": GROUP_ID},
            headers={"X-Engine-Secret": "wrong-secret"},
        )

    assert res.status_code == 401
    assert res.json() == {"detail": {"error": "invalid_secret"}}


def test_assign_group_with_valid_secret_registers(
    mock_app, httpx_mock: HTTPXMock
) -> None:
    """올바른 시크릿이면 groupId 를 저장하고 BE /register-dual 을 호출함"""
    httpx_mock.add_response(method="POST", status_code=200, json={"status": "ok"})

    with TestClient(mock_app.app) as client:
        res = client.post(
            "/control/assign-group",
            json={"group_id": GROUP_ID},
            headers={"X-Engine-Secret": DEFAULT_SECRET},
        )

    assert res.status_code == 200
    assert res.json()["groupId"] == GROUP_ID

    register = [r for r in httpx_mock.get_requests() if "register-dual" in str(r.url)]
    assert register, "BE /register-dual 호출이 없음"
