"""start_heartbeat_dual pytest — DUAL heartbeat 신규 함수 검증함 (Phase 17.5)"""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from server.config import settings
from server.services import webhook
from server.services.webhook import start_heartbeat_dual

TEST_GROUP_ID = "507f1f77bcf86cd799439011"
BACKEND_DUAL_URL = f"{settings.backend_url}/api/engine/register-dual"


@pytest.mark.asyncio
async def test_start_heartbeat_dual_calls_register_to_backend_dual(httpx_mock):
    """heartbeat 한 tick 후 register_to_backend_dual 호출 확인함"""
    # 대기 시간 안에 tick이 2회 이상 발생하므로 응답을 재사용 가능하게 등록함
    httpx_mock.add_response(
        url=BACKEND_DUAL_URL, method="POST", status_code=200, is_reusable=True
    )

    # HEARTBEAT_INTERVAL_SEC을 0.05s로 monkeypatch — 1 tick 빠르게 트리거함
    with patch.object(webhook, "HEARTBEAT_INTERVAL_SEC", 0.05):
        task = asyncio.create_task(
            start_heartbeat_dual(
                "http://testhost:5002", TEST_GROUP_ID, 1, "test-secret"
            )
        )
        # 1 tick 이상 대기함
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # register-dual 호출 1회 이상 발생 확인함
    requests = httpx_mock.get_requests(url=BACKEND_DUAL_URL)
    assert len(requests) >= 1


@pytest.mark.asyncio
async def test_start_heartbeat_dual_handles_transient_errors(
    httpx_mock: HTTPXMock,
):
    """httpx.RequestError 발생 시 log-and-continue — task 죽지 않음 확인함"""
    # 하나의 exception만 설정 — 남은 mock 검증 실패 회피함
    httpx_mock.add_exception(httpx.ConnectTimeout("simulated timeout"))

    with patch.object(webhook, "HEARTBEAT_INTERVAL_SEC", 0.02):
        task = asyncio.create_task(
            start_heartbeat_dual(
                "http://testhost:5002", TEST_GROUP_ID, 1, "test-secret"
            )
        )
        # 1 tick 대기함
        await asyncio.sleep(0.04)
        # exception 발생 후에도 task가 살아있는지 확인함 (log-and-continue 핵심)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # exception도 request로 카운트됨 — 최소 1회 호출 발생
    requests = httpx_mock.get_requests(url=BACKEND_DUAL_URL)
    assert len(requests) >= 1
