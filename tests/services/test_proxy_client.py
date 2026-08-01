"""proxy_client.py 단위 테스트 모음 — TDD RED → GREEN"""

import inspect
import json
import threading
from typing import Any, Generator

import pytest
from pytest_httpx import HTTPXMock

# ──────────────────────────────────────────────
# 테스트 상수
# ──────────────────────────────────────────────
MOCK_PROXY_URL = "http://mock-proxy:3100"
MOCK_SECRET_KEY = "de-secret"
MOCK_GROUP_ID = "507f1f77bcf86cd799439011"
MOCK_SUBJECT_IDX = 1
MOCK_SEQ = 42
MOCK_PAYLOAD = {
    "delta": 0.1,
    "theta": 0.2,
    "alpha": 0.3,
    "beta": 0.4,
    "gamma": 0.5,
}
MOCK_SYNC_META = {"de_clock_domain": "monotonic_ns"}
INGEST_SAMPLE_URL = f"{MOCK_PROXY_URL}/ingest/sample"
FIXED_TS_NS = 123456789


# ──────────────────────────────────────────────
# T-PC-1: 성공 — 올바른 URL / 헤더 / body / de_ts_ns 확인
# ──────────────────────────────────────────────
def test_post_sample_success(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """200 OK 시 URL·헤더·body·de_ts_ns 검증 — proxy_ingress_ts_ns 부재 확인함"""
    from server.services import proxy_client

    # time.monotonic_ns 고정함
    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)

    httpx_mock.add_response(
        url=INGEST_SAMPLE_URL,
        method="POST",
        json={},
        status_code=200,
    )

    proxy_client.post_sample(
        proxy_url=MOCK_PROXY_URL,
        secret_key=MOCK_SECRET_KEY,
        group_id=MOCK_GROUP_ID,
        subject_idx=MOCK_SUBJECT_IDX,
        seq=MOCK_SEQ,
        payload=MOCK_PAYLOAD,
        sync_meta=MOCK_SYNC_META,
    )

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "POST"
    assert str(request.url) == INGEST_SAMPLE_URL

    # X-Engine-Secret 헤더 검증함
    assert request.headers.get("x-engine-secret") == MOCK_SECRET_KEY

    # body 검증함
    body = json.loads(request.content)
    assert body["group_id"] == MOCK_GROUP_ID
    assert body["subject_idx"] == MOCK_SUBJECT_IDX
    assert body["de_ts_ns"] == str(FIXED_TS_NS)  # 십진수 문자열임
    assert body["seq"] == MOCK_SEQ
    assert body["payload"] == MOCK_PAYLOAD
    assert body["sync_meta"] == MOCK_SYNC_META
    assert body["sync_meta"]["de_clock_domain"] == "monotonic_ns"

    # proxy_ingress_ts_ns는 프록시가 추가 — DE body에서 부재 확인함
    assert "proxy_ingress_ts_ns" not in body


# ──────────────────────────────────────────────
# T-PC-2: retry 후 성공 — 503 1회 + 200 → 성공, sleep 1회 확인
# ──────────────────────────────────────────────
def test_post_sample_retry_then_success(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """503 1회 후 200 → 성공 확인, time.sleep(0.1) 1회 호출 검증함"""
    from server.services import proxy_client

    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)

    sleep_calls: list[float] = []

    def mock_sleep(seconds: float) -> None:
        # 실제 sleep 없이 호출 기록만 수행함
        sleep_calls.append(seconds)

    monkeypatch.setattr(proxy_client.time, "sleep", mock_sleep)

    # 503 1회 → 200 1회
    httpx_mock.add_response(
        url=INGEST_SAMPLE_URL,
        method="POST",
        status_code=503,
        json={"error": "Service Unavailable"},
    )
    httpx_mock.add_response(
        url=INGEST_SAMPLE_URL,
        method="POST",
        json={},
        status_code=200,
    )

    # max_retries=2 기본값으로 호출함 (1st retry에 0.1s backoff)
    proxy_client.post_sample(
        proxy_url=MOCK_PROXY_URL,
        secret_key=MOCK_SECRET_KEY,
        group_id=MOCK_GROUP_ID,
        subject_idx=MOCK_SUBJECT_IDX,
        seq=MOCK_SEQ,
        payload=MOCK_PAYLOAD,
        sync_meta=MOCK_SYNC_META,
    )

    # 요청 2회 발생 확인함 (1st 503, 2nd 200)
    requests = httpx_mock.get_requests(url=INGEST_SAMPLE_URL)
    assert len(requests) == 2

    # sleep 1회 / 0.1s backoff 확인함
    assert sleep_calls == [0.1]


# ──────────────────────────────────────────────
# T-PC-3: retry 소진 → ProxyForwardError + sleep 순서 + Redis 미포함 확인
# ──────────────────────────────────────────────
def test_post_sample_503_retry_exhausted_raises(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """503 3회(1+max_retries=2) → ProxyForwardError raise, sleep [0.1, 0.2] 확인함"""
    from server.services import proxy_client
    from server.services.proxy_client import ProxyForwardError

    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)

    sleep_calls: list[float] = []

    def mock_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(proxy_client.time, "sleep", mock_sleep)

    # 총 1+max_retries=3회 503 응답 등록함
    for _ in range(3):
        httpx_mock.add_response(
            url=INGEST_SAMPLE_URL,
            method="POST",
            status_code=503,
            json={"error": "Service Unavailable"},
        )

    with pytest.raises(ProxyForwardError):
        proxy_client.post_sample(
            proxy_url=MOCK_PROXY_URL,
            secret_key=MOCK_SECRET_KEY,
            group_id=MOCK_GROUP_ID,
            subject_idx=MOCK_SUBJECT_IDX,
            seq=MOCK_SEQ,
            payload=MOCK_PAYLOAD,
            sync_meta=MOCK_SYNC_META,
            max_retries=2,
        )

    # 총 요청 3회 확인함 (1 + max_retries=2)
    requests = httpx_mock.get_requests(url=INGEST_SAMPLE_URL)
    assert len(requests) == 3

    # 지수 백오프 순서 확인함 (0.1 → 0.2)
    assert sleep_calls == [0.1, 0.2]

    # Redis 미포함 검증 — proxy_client 모듈에 redis import 부재 확인함
    source = inspect.getsource(proxy_client)
    assert "redis" not in source.lower(), "proxy_client에 redis import가 있어서는 안 됨"


# ──────────────────────────────────────────────
# T1-DE-C: check_health 단위 테스트 (pytest-httpx)
# ──────────────────────────────────────────────
HEALTH_URL = f"{MOCK_PROXY_URL}/health"


def test_check_health_200_ok_returns_true(httpx_mock: HTTPXMock) -> None:
    """200 응답 시 True 반환 확인함"""
    from server.services.proxy_client import check_health

    httpx_mock.add_response(
        url=HEALTH_URL,
        method="GET",
        status_code=200,
        json={"status": "ok", "version": "1.0.0"},
    )

    result = check_health(MOCK_PROXY_URL)
    assert result is True


def test_check_health_503_fail_closed_returns_false(httpx_mock: HTTPXMock) -> None:
    """503 응답 시 False 반환 확인함"""
    from server.services.proxy_client import check_health

    httpx_mock.add_response(
        url=HEALTH_URL,
        method="GET",
        status_code=503,
        json={"status": "fail_closed", "version": "1.0.0"},
    )

    result = check_health(MOCK_PROXY_URL)
    assert result is False


def test_check_health_connection_error_returns_false(httpx_mock: HTTPXMock) -> None:
    """연결 오류 시 예외 미발생, False 반환 확인함"""
    import httpx as _httpx

    from server.services.proxy_client import check_health

    httpx_mock.add_exception(
        _httpx.ConnectError("Connection refused"),
        url=HEALTH_URL,
        method="GET",
    )

    result = check_health(MOCK_PROXY_URL)
    assert result is False


# ──────────────────────────────────────────────
# T1-DE-C: ProxyHealthTracker 순수 단위 테스트 (모킹 없음)
# ──────────────────────────────────────────────


def test_tracker_healthy_returns_false_and_resets() -> None:
    """healthy=True 시 False 반환 및 내부 상태 reset 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    # healthy 상태에서는 항상 False 반환함
    assert tracker.record(True, 0.0) is False
    assert tracker.record(True, 100.0) is False


def test_tracker_first_fail_returns_false() -> None:
    """최초 fail 기록 시 False 반환 확인함 (threshold 미도달)"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    # 첫 번째 fail — threshold 미도달임
    assert tracker.record(False, 0.0) is False


def test_tracker_fail_below_threshold_returns_false() -> None:
    """fail 지속 시간이 threshold 미만이면 False 반환 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)  # _first_fail_sec = 0.0
    # 2999ms 경과 — threshold 미도달임
    assert tracker.record(False, 2.999) is False


def test_tracker_fail_at_threshold_returns_true() -> None:
    """fail 지속 시간이 threshold 정확히 도달하면 True 반환 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)  # _first_fail_sec = 0.0
    # 3000ms 정확히 도달 — True 반환해야 함
    assert tracker.record(False, 3.0) is True


def test_tracker_latched_true_on_continued_fail() -> None:
    """threshold 초과 후 계속 fail 시 True 유지(latch) 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)
    tracker.record(False, 3.0)  # trip
    # 이후 추가 fail 에서도 True 유지됨
    assert tracker.record(False, 4.0) is True
    assert tracker.record(False, 10.0) is True


def test_tracker_healthy_after_trip_resets_to_false() -> None:
    """trip 후 healthy=True 입력 시 상태 reset → False 반환 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)
    tracker.record(False, 3.0)  # trip
    # healthy 복구 시 reset됨
    assert tracker.record(True, 4.0) is False
    # 재차 fail 시 새로 카운트 시작함
    assert tracker.record(False, 5.0) is False  # 첫 fail — threshold 미도달
    assert tracker.record(False, 7.999) is False  # 2999ms 미도달
    assert tracker.record(False, 8.0) is True  # 3000ms 도달 — True


# ──────────────────────────────────────────────
# T-PC-8: Client 재사용 회귀 (EEG-W003)
#
# 결함: post_sample이 호출마다 httpx.Client를 새로 만들어 매번 SSL 컨텍스트를
# 재구성했고, PC A 실측 약 690ms가 소요됐음. 128샘플 블록당 1초 예산을 잠식해
# Cortex 수신 백로그가 초당 약 0.2초씩 누적됐음(10분 측정에 약 100초 지연).
# 실측 근거: httpx.Client() 생성만 694ms, 재사용 시 POST 왕복 5.3ms.
# 아래 테스트는 "호출 횟수와 무관하게 Client 생성은 1회"를 잠금.
# ──────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_shared_client() -> Generator[None, None, None]:
    """테스트 간 공용 Client 격리함 — mock transport가 새 Client에 적용되게 함.

    getattr 폴백은 공용 Client가 없는 구현에서도 나머지 테스트가 정상 실행되어
    본 회귀 테스트가 AttributeError가 아니라 생성 횟수로 실패하게 하려는 것임.
    """
    from server.services import proxy_client

    reset = getattr(proxy_client, "close_shared_client", lambda: None)
    reset()
    yield
    reset()


def _count_client_constructions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """httpx.Client 생성 횟수를 세는 카운터 설치하고 리스트로 반환함"""
    import httpx

    from server.services import proxy_client

    calls = [0]
    real_client = httpx.Client

    def counting_client(*args: Any, **kwargs: Any) -> httpx.Client:
        calls[0] += 1
        return real_client(*args, **kwargs)

    monkeypatch.setattr(proxy_client.httpx, "Client", counting_client)
    return calls


def test_post_sample_reuses_single_client(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """post_sample 3회 호출 시 httpx.Client 생성은 1회뿐임을 확인함"""
    from server.services import proxy_client

    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)
    calls = _count_client_constructions(monkeypatch)

    for _ in range(3):
        httpx_mock.add_response(url=INGEST_SAMPLE_URL, method="POST", status_code=200)

    for seq in range(3):
        proxy_client.post_sample(
            proxy_url=MOCK_PROXY_URL,
            secret_key=MOCK_SECRET_KEY,
            group_id=MOCK_GROUP_ID,
            subject_idx=MOCK_SUBJECT_IDX,
            seq=seq,
            payload=MOCK_PAYLOAD,
            sync_meta=MOCK_SYNC_META,
        )

    assert len(httpx_mock.get_requests(url=INGEST_SAMPLE_URL)) == 3
    assert calls[0] == 1, f"Client 생성이 {calls[0]}회 — 호출마다 생성하면 안 됨"


def test_check_health_reuses_same_client_as_post_sample(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """check_health와 post_sample이 같은 Client를 공유함을 확인함"""
    from server.services import proxy_client

    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)
    calls = _count_client_constructions(monkeypatch)

    httpx_mock.add_response(
        url=f"{MOCK_PROXY_URL}/health", method="GET", status_code=200
    )
    httpx_mock.add_response(url=INGEST_SAMPLE_URL, method="POST", status_code=200)

    assert proxy_client.check_health(MOCK_PROXY_URL) is True
    proxy_client.post_sample(
        proxy_url=MOCK_PROXY_URL,
        secret_key=MOCK_SECRET_KEY,
        group_id=MOCK_GROUP_ID,
        subject_idx=MOCK_SUBJECT_IDX,
        seq=MOCK_SEQ,
        payload=MOCK_PAYLOAD,
        sync_meta=MOCK_SYNC_META,
    )

    assert calls[0] == 1, f"Client 생성이 {calls[0]}회 — 두 경로가 공유해야 함"


def test_concurrent_get_shared_client_constructs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """여러 스레드가 동시에 진입해도 Client는 1개만 생성됨을 확인함.

    get_shared_client가 락 없이 None 검사와 대입을 하면 첫 호출들이 겹칠 때
    각자 Client를 만들고 마지막 하나만 남아 나머지가 누수됨. Barrier로 동시
    진입을 강제해 그 경합을 재현함.
    """
    from server.services import proxy_client

    # 실물 Client 대신 더미를 세움 — 실물 생성은 CA 번들을 읽어 파일시스템에
    # 의존하고, 이 테스트는 HTTP가 아니라 생성 횟수와 동일성만 검증함
    calls = [0]

    class _DummyClient:
        is_closed = False

        def close(self) -> None:
            pass

    def counting_client(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return _DummyClient()

    monkeypatch.setattr(proxy_client.httpx, "Client", counting_client)

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    results: list[object] = [None] * thread_count
    errors: list[tuple[int, BaseException]] = []

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            results[idx] = proxy_client.get_shared_client()
        except BaseException as exc:  # 진단 목적으로 캡처함
            errors.append((idx, exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"워커 스레드에서 예외 발생: {errors}"
    assert all(
        result is results[0] for result in results
    ), "스레드마다 다른 Client 반환됨"
    assert calls[0] == 1, f"Client 생성이 {calls[0]}회 — 동시 진입에도 1회여야 함"


def test_close_shared_client_allows_recreation() -> None:
    """close 후 재요청 시 새 Client가 생성되어 재사용 가능함을 확인함"""
    from server.services import proxy_client

    first = proxy_client.get_shared_client()
    assert proxy_client.get_shared_client() is first

    proxy_client.close_shared_client()

    second = proxy_client.get_shared_client()
    assert second is not first
    assert second.is_closed is False
