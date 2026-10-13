"""MESSAGE_BUS=memory 동작 테스트.

Redis 없이 샘플과 헤드셋 상태 경보를 프록시로 보내는 경로를 잠금.
기본값(redis)의 동작은 기존 테스트가 그대로 덮고, 여기서는 기본값에서 Redis
클라이언트가 여전히 만들어지는지만 대조군으로 확인함.
"""

import importlib
import json
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from pytest_httpx import HTTPXMock

import core.main as main_module
import core.streamer as streamer_module
from core.streamer import MindSignalStreamer, StreamContinuityTracker
from sdk.cortex import Cortex
from server.config import MessageBusConfigError, settings, validate_message_bus
from server.services import proxy_client
from server.services.proxy_client import ProxyForwardError

PROXY_URL = "http://mock-proxy:5050"
SECRET = "de-secret"
EEG_LABELS = ["COUNTER", "INTERPOLATED", "AF3", "T7", "Pz", "T8", "AF4"]


# ──────────────────────────────────────────────
# 대역과 헬퍼
# ──────────────────────────────────────────────
def _patch_constructor_side_effects(monkeypatch, tmp_path) -> dict:
    """Cortex 연결, 시그널 등록, CSV 경로 부작용 없이 __init__을 돌리게 함."""
    calls = {"cortex_init": 0, "from_url": 0}

    def fake_cortex_init(self, *args, **kwargs):
        calls["cortex_init"] += 1

    def fake_from_url(*args, **kwargs):
        calls["from_url"] += 1
        return SimpleNamespace(publish=lambda *a, **k: None)

    monkeypatch.setattr(Cortex, "__init__", fake_cortex_init)
    monkeypatch.setattr(MindSignalStreamer, "bind", lambda self, **kw: None)
    monkeypatch.setattr(streamer_module.signal, "signal", lambda *a: None)
    monkeypatch.setattr(streamer_module.redis, "from_url", fake_from_url)
    # base_path(parent x3)가 tmp_path를 가리키게 해 실제 csv 폴더를 건드리지 않음
    monkeypatch.setattr(
        streamer_module,
        "Path",
        lambda _file: SimpleNamespace(resolve=lambda: tmp_path / "a" / "b" / "c"),
    )
    return calls


def _memory_streamer(proxy_fail_closed: bool = True) -> MindSignalStreamer:
    """Cortex 부작용 없이 memory 버스 경로만 돌리기 위한 인스턴스 생성함."""
    s = MindSignalStreamer.__new__(MindSignalStreamer)
    s.message_bus = "memory"
    s.proxy_fail_closed = proxy_fail_closed
    s.alignment_location = "proxy"
    s.proxy_url = PROXY_URL
    s.engine_secret_key = SECRET
    s.max_proxy_post_retries = 0
    s.r = None
    s.group_id = "g1"
    s.subject_index = 1
    s.channel = "mind-signal:g1:subject:1"
    s.start_time = s.last_data_time = s.last_met_time = 0.0
    s.eeg_channel_indices = []
    s.eeg_channel_names = []
    s.eeg_buffer = []
    s.analyzer = SimpleNamespace(fs=128)
    s.counter_index = None
    s.interpolated_index = None
    s.continuity = StreamContinuityTracker()
    s.csv_rows_written = 0
    s.dropped_samples = 0
    s.dropped_blocks = 0
    s.seq = 0
    s._continuity_log_counts = {}
    s._health_published_at = {}
    s._last_event_monotonic = None
    s._eeg_stale = False
    s._met_stale = False
    s._fail_closed_triggered = False
    s.latest_met = dict.fromkeys(
        ["focus", "engagement", "interest", "excitement", "stress", "relaxation"], 0
    )
    s.on_new_data_labels(data={"streamName": "eeg", "labels": EEG_LABELS})
    return s


def _record_post_status(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(streamer_module, "post_status", lambda **kw: sent.append(kw))
    return sent


# ──────────────────────────────────────────────
# 1. memory 버스는 Redis 클라이언트를 만들지 않음
# ──────────────────────────────────────────────
def test_memory_bus_does_not_create_redis_client(monkeypatch, tmp_path) -> None:
    calls = _patch_constructor_side_effects(monkeypatch, tmp_path)

    s = MindSignalStreamer(
        "g1",
        "1",
        alignment_location="proxy",
        proxy_url=PROXY_URL,
        message_bus="memory",
    )
    s.csv_file.close()

    assert calls["from_url"] == 0
    assert s.r is None
    assert s.message_bus == "memory"


def test_default_bus_still_creates_redis_client(monkeypatch, tmp_path) -> None:
    """대조군 — 기본값(redis)은 기존대로 생성 시점에 클라이언트를 만듦."""
    calls = _patch_constructor_side_effects(monkeypatch, tmp_path)

    s = MindSignalStreamer("g1", "1")
    s.csv_file.close()

    assert calls["from_url"] == 1
    assert s.message_bus == "redis"
    assert s.proxy_fail_closed is True


# ──────────────────────────────────────────────
# 2. 설정이 잘못되면 측정 시작 전에 즉시 멈춤
# ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("message_bus", "alignment_location", "proxy_url", "expected"),
    [
        ("memory", "be", PROXY_URL, "ALIGNMENT_LOCATION=proxy"),
        ("memory", "", PROXY_URL, "ALIGNMENT_LOCATION=proxy"),
        ("memory", "proxy", None, "PROXY_URL"),
        ("memory", "proxy", "", "PROXY_URL"),
        ("kafka", "proxy", PROXY_URL, "MESSAGE_BUS='kafka'"),
    ],
)
def test_invalid_message_bus_config_raises_with_cause(
    message_bus, alignment_location, proxy_url, expected
) -> None:
    with pytest.raises(MessageBusConfigError, match=expected):
        validate_message_bus(message_bus, alignment_location, proxy_url)


@pytest.mark.parametrize(
    ("alignment_location", "proxy_url"),
    [("be", None), ("proxy", None), ("proxy", PROXY_URL)],
)
def test_redis_bus_accepts_every_existing_combination(
    alignment_location, proxy_url
) -> None:
    validate_message_bus("redis", alignment_location, proxy_url)


def test_streamer_rejects_bad_memory_config_before_cortex(
    monkeypatch, tmp_path
) -> None:
    """Cortex 연결과 CSV 생성보다 먼저 멈춰야 함."""
    calls = _patch_constructor_side_effects(monkeypatch, tmp_path)

    with pytest.raises(MessageBusConfigError):
        MindSignalStreamer("g1", "1", alignment_location="be", message_bus="memory")

    assert calls["cortex_init"] == 0
    assert calls["from_url"] == 0
    assert list(tmp_path.iterdir()) == []


def test_main_exits_with_cause_on_bad_memory_config(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MESSAGE_BUS", "memory")
    monkeypatch.setenv("ALIGNMENT_LOCATION", "proxy")
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["core.main", "group-xyz", "1"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert "PROXY_URL" in capsys.readouterr().out


# ──────────────────────────────────────────────
# 3. 경보가 프록시로 감
# ──────────────────────────────────────────────
def test_watchdog_alert_goes_to_proxy(monkeypatch) -> None:
    sent = _record_post_status(monkeypatch)
    s = _memory_streamer()

    s._publish_health("no_data", 31.4)

    assert sent == [
        {
            "proxy_url": PROXY_URL,
            "secret_key": SECRET,
            "group_id": "g1",
            "subject_idx": 1,
            "status": "no_data",
            "silent_seconds": 31,
            "timeout": streamer_module.STATUS_POST_TIMEOUT_SEC,
        }
    ]


def test_disconnected_alert_goes_to_proxy_without_silent_seconds(monkeypatch) -> None:
    sent = _record_post_status(monkeypatch)
    s = _memory_streamer()

    s.on_headset_disconnected()

    assert len(sent) == 1
    assert sent[0]["status"] == "disconnected"
    assert sent[0]["silent_seconds"] is None


def test_alert_throttle_applies_to_proxy_path(monkeypatch) -> None:
    sent = _record_post_status(monkeypatch)
    s = _memory_streamer()

    s._publish_health("invalid_data", min_interval_sec=10)
    s._publish_health("invalid_data", min_interval_sec=10)

    assert len(sent) == 1


def test_post_status_request_shape(httpx_mock: HTTPXMock) -> None:
    """프록시 /status 계약 — 주소, 헤더, 본문 네 필드."""
    httpx_mock.add_response(url=f"{PROXY_URL}/status", method="POST", json={"ok": True})

    proxy_client.post_status(PROXY_URL, SECRET, "g1", 1, "no_data", silent_seconds=31)

    request = httpx_mock.get_request()
    assert request.headers["x-engine-secret"] == SECRET
    assert json.loads(request.content) == {
        "group_id": "g1",
        "subject_idx": 1,
        "status": "no_data",
        "silent_seconds": 31,
    }


def test_post_status_omits_silent_seconds_when_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{PROXY_URL}/status", method="POST", json={"ok": True})

    proxy_client.post_status(PROXY_URL, SECRET, "g1", 2, "disconnected")

    assert json.loads(httpx_mock.get_request().content) == {
        "group_id": "g1",
        "subject_idx": 2,
        "status": "disconnected",
    }


def test_post_status_raises_on_error_response_without_retry(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=f"{PROXY_URL}/status", method="POST", status_code=503)

    with pytest.raises(httpx.HTTPStatusError):
        proxy_client.post_status(PROXY_URL, SECRET, "g1", 1, "no_data")

    assert len(httpx_mock.get_requests()) == 1


# ──────────────────────────────────────────────
# 4. 경보 전송 실패가 측정을 중단시키지 않음
# ──────────────────────────────────────────────
def test_alert_failure_does_not_break_measurement(monkeypatch, caplog) -> None:
    def boom(**kwargs):
        raise httpx.ConnectError("proxy down")

    monkeypatch.setattr(streamer_module, "post_status", boom)
    s = _memory_streamer()

    with caplog.at_level("WARNING", logger="core.streamer"):
        s.on_eeg_data_done(
            data={"eeg": [0, 0, float("nan"), 1.0, 1.0, 1.0, 1.0], "time": 1000.0}
        )
        s.on_eeg_data_done(
            data={"eeg": [1, 0, 1.0, 2.0, 3.0, 4.0, 5.0], "time": 1000.01}
        )
        s.on_headset_disconnected()

    assert s.dropped_samples == 1
    assert s.eeg_buffer == [[1.0, 2.0, 3.0, 4.0, 5.0]]
    assert sum("상태 프록시 전송 실패" in r.message for r in caplog.records) == 2


# ──────────────────────────────────────────────
# 5. 1인 memory 측정은 프록시 장애로 측정을 끊지 않음
# ──────────────────────────────────────────────
def _feed_one_block(s: MindSignalStreamer, monkeypatch, rows: list) -> None:
    """128샘플 1블록을 넣어 샘플 발행 분기까지 도달시킴."""
    s.analyzer = SimpleNamespace(
        fs=128,
        get_all_powers=lambda _x: dict.fromkeys(streamer_module.BAND_NAMES, 1.0),
    )
    s.writer = SimpleNamespace(writerow=rows.append)
    s.csv_file = SimpleNamespace(flush=lambda: None)
    closed: list[str] = []
    s.close_session = lambda: closed.append("close_session")
    s.close = lambda: closed.append("close")
    s._closed_calls = closed
    values = np.ones(5).tolist()
    for i in range(128):
        s.on_eeg_data_done(data={"eeg": [i, 0, *values], "time": 1000.0 + i / 128})


def _failing_post_sample(**kwargs):
    raise ProxyForwardError("proxy 전송 retry 소진")


def test_solo_memory_proxy_failure_keeps_measuring(monkeypatch, caplog) -> None:
    monkeypatch.setattr(streamer_module, "post_sample", _failing_post_sample)
    s = _memory_streamer(proxy_fail_closed=False)
    rows: list = []

    with caplog.at_level("WARNING", logger="core.streamer"):
        _feed_one_block(s, monkeypatch, rows)
        _feed_one_block(s, monkeypatch, rows)

    assert len(rows) == 2
    assert s.csv_rows_written == 2
    assert s._closed_calls == []
    assert s._fail_closed_triggered is False
    assert s.eeg_buffer == []
    assert any("CSV 저장은 계속" in r.message for r in caplog.records)


def test_dual_memory_proxy_failure_still_fails_closed(monkeypatch) -> None:
    """대조군 — DUAL_2PC(기본 proxy_fail_closed=True)의 중단 동작은 그대로임."""
    seen: list[dict] = []

    def failing(**kwargs):
        seen.append(kwargs)
        raise ProxyForwardError("proxy 전송 retry 소진")

    monkeypatch.setattr(streamer_module, "post_sample", failing)
    s = _memory_streamer(proxy_fail_closed=True)
    s.max_proxy_post_retries = 2

    _feed_one_block(s, monkeypatch, [])

    assert s._fail_closed_triggered is True
    assert s._closed_calls == ["close_session", "close"]
    # fail-closed 경로의 재시도 횟수와 시간 제한은 기존 그대로임
    assert seen[0]["max_retries"] == 2
    assert "timeout" not in seen[0]


def test_solo_memory_sends_once_with_short_timeout(monkeypatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(streamer_module, "post_sample", lambda **kw: seen.append(kw))
    s = _memory_streamer(proxy_fail_closed=False)
    s.max_proxy_post_retries = 2

    _feed_one_block(s, monkeypatch, [])

    assert seen[0]["max_retries"] == 0
    assert seen[0]["timeout"] == streamer_module.SOLO_SAMPLE_POST_TIMEOUT_SEC
    assert streamer_module.SOLO_SAMPLE_POST_TIMEOUT_SEC <= 1.0


def test_solo_memory_slow_proxy_does_not_stall_csv(monkeypatch) -> None:
    """응답 없는 프록시가 블록 하나를 붙잡는 시간이 한도 안이고, 그 뒤 건너뜀.

    연결은 받되 응답하지 않는 소켓으로 실제 httpx 시간 제한을 태움. 수정 전에는
    10초 x 3회라 블록당 약 30초였음.
    """
    injected_timeout = 0.1
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    calls: list[dict] = []
    real_post_sample = proxy_client.post_sample

    def spy(**kwargs):
        calls.append(kwargs)
        real_post_sample(**kwargs)

    monkeypatch.setattr(streamer_module, "post_sample", spy)
    s = _memory_streamer(proxy_fail_closed=False)
    s.proxy_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    s.solo_sample_post_timeout_sec = injected_timeout
    rows: list = []

    try:
        started = time.monotonic()
        _feed_one_block(s, monkeypatch, rows)
        first_block_sec = time.monotonic() - started

        started = time.monotonic()
        for _ in range(3):
            _feed_one_block(s, monkeypatch, rows)
        skipped_blocks_sec = time.monotonic() - started
    finally:
        proxy_client.close_shared_client()
        listener.close()

    # 한도는 연결과 읽기 합산 약 2배에 블록 계산 여유를 더한 값임
    # 상한만 봄. 하한은 실제 소켓 시간 제한을 재는 것이라 시계 해상도가 낮은
    # 환경에서 흔들릴 수 있고, 전송 시도 1회는 아래 호출 수 단언이 확인함
    assert first_block_sec < injected_timeout * 2 + 1.0
    assert len(calls) == 1, "건너뛰는 구간에서 전송을 시도하면 안 됨"
    assert skipped_blocks_sec < 1.0
    assert len(rows) == 4
    assert s._closed_calls == []


def test_solo_memory_resumes_sending_after_skip_window(monkeypatch) -> None:
    monkeypatch.setattr(streamer_module, "post_sample", _failing_post_sample)
    s = _memory_streamer(proxy_fail_closed=False)
    _feed_one_block(s, monkeypatch, [])
    assert s._proxy_skip_until > time.monotonic()

    seen: list[dict] = []
    monkeypatch.setattr(streamer_module, "post_sample", lambda **kw: seen.append(kw))
    s._proxy_skip_until = time.monotonic() - 0.001
    _feed_one_block(s, monkeypatch, [])

    assert len(seen) == 1


@pytest.mark.parametrize(
    ("proxy_fail_closed", "expected_starts"), [(False, 0), (True, 1)]
)
def test_health_monitor_starts_only_when_fail_closed(
    monkeypatch, proxy_fail_closed, expected_starts
) -> None:
    s = _memory_streamer(proxy_fail_closed=proxy_fail_closed)
    s.duration_min = 1
    s.duration_sec = 3600
    starts: list[int] = []
    s._start_proxy_health_monitor = lambda: starts.append(1)
    s._start_watchdog = lambda: None
    s._start_rollup_thread = lambda: None
    s.sub_request = lambda streams: None
    monkeypatch.setattr(
        streamer_module.threading,
        "Timer",
        lambda *a, **k: SimpleNamespace(daemon=False, start=lambda: None),
    )

    s.on_create_session_done()

    assert len(starts) == expected_starts


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"MESSAGE_BUS": "memory"}, False),
        ({"MESSAGE_BUS": "memory", "DUAL_2PC_SUBJECT_INDEX": "1"}, True),
        (
            {
                "MESSAGE_BUS": "memory",
                "DUAL_2PC_GROUP_ID": "g",
                "DUAL_2PC_SUBJECT_INDEX": "1",
            },
            True,
        ),
        ({"MESSAGE_BUS": "redis"}, True),
        ({}, True),
        ({"MESSAGE_BUS": ""}, True),
    ],
)
def test_main_disables_fail_closed_only_for_solo_memory(
    monkeypatch, env, expected
) -> None:
    captured: dict = {}

    class FakeStreamer:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def open(self):
            pass

    monkeypatch.setattr(main_module, "MindSignalStreamer", FakeStreamer)
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    for key in ("MESSAGE_BUS", "DUAL_2PC_GROUP_ID", "DUAL_2PC_SUBJECT_INDEX"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["core.main", "group-xyz", "1"])

    main_module.main()

    assert captured["proxy_fail_closed"] is expected
    assert captured["message_bus"] == (env.get("MESSAGE_BUS") or "redis")


# ──────────────────────────────────────────────
# 6. lifespan — 1인 memory 측정은 1PC legacy 백엔드 등록을 수행함
# ──────────────────────────────────────────────
async def _run_lifespan_with(monkeypatch, **overrides) -> dict:
    """등록 함수를 대역으로 바꾼 뒤 lifespan을 1회 돌리고 호출 기록 반환함."""
    base = {
        "engine_secret_key": "test-valid-secret",
        "registration_mode": "local",
        "lan_ip": "127.0.0.1",
        "dual_2pc_group_id": None,
        "dual_2pc_subject_index": None,
        "alignment_location": "proxy",
        "proxy_url": PROXY_URL,
        "message_bus": "memory",
        "proxy_reregister_interval_sec": 3600,
    }
    for key, value in {**base, **overrides}.items():
        monkeypatch.setattr(settings, key, value)

    import server.app as app_module

    mod = importlib.reload(app_module)
    mocks = {
        name: AsyncMock()
        for name in (
            "register_to_backend",
            "register_to_proxy",
            "start_heartbeat",
            "register_to_backend_pending",
            "unregister_to_backend_pending",
        )
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(mod, name, mock)

    async with mod.app.router.lifespan_context(mod.app):
        mocks["heartbeat_task_created"] = mod.app.state.heartbeat_task is not None
    return mocks


@pytest.mark.asyncio
async def test_lifespan_solo_memory_registers_legacy_slot(monkeypatch) -> None:
    mocks = await _run_lifespan_with(monkeypatch)

    mocks["register_to_backend"].assert_awaited_once()
    assert mocks["heartbeat_task_created"] is True
    mocks["register_to_proxy"].assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_dual_memory_keeps_proxy_registration(monkeypatch) -> None:
    """대조군 — DUAL_2PC 설정이 있으면 memory여도 기존 proxy 등록 분기 그대로임."""
    mocks = await _run_lifespan_with(monkeypatch, dual_2pc_subject_index=1)

    mocks["register_to_proxy"].assert_awaited_once()
    mocks["register_to_backend"].assert_not_awaited()
    assert mocks["heartbeat_task_created"] is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"alignment_location": "be"}, "ALIGNMENT_LOCATION=proxy"),
        ({"proxy_url": None}, "PROXY_URL"),
        ({"message_bus": "kafka"}, "MESSAGE_BUS='kafka'"),
    ],
)
@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_on_bad_memory_config(
    monkeypatch, capsys, overrides, expected
) -> None:
    """잘못된 조합이면 등록 전에 서버 기동을 멈추고 원인을 출력함."""
    with pytest.raises(SystemExit) as exc_info:
        await _run_lifespan_with(monkeypatch, **overrides)

    assert exc_info.value.code == 1
    assert expected in capsys.readouterr().out


@pytest.mark.asyncio
async def test_lifespan_redis_bus_starts_without_proxy_settings(monkeypatch) -> None:
    """대조군 — 기본 버스는 프록시 설정이 없어도 기존대로 기동하고 등록함."""
    mocks = await _run_lifespan_with(
        monkeypatch, message_bus="redis", alignment_location="be", proxy_url=None
    )

    mocks["register_to_backend"].assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_solo_redis_proxy_mode_unchanged(monkeypatch) -> None:
    """대조군 — 기본 버스(redis)의 proxy 모드는 기존대로 백엔드 등록을 건너뜀."""
    mocks = await _run_lifespan_with(monkeypatch, message_bus="redis")

    mocks["register_to_backend"].assert_not_awaited()
    mocks["register_to_proxy"].assert_not_awaited()
    assert mocks["heartbeat_task_created"] is False


def test_empty_dual_2pc_env_is_treated_as_unset(monkeypatch):
    """.env.example처럼 값이 빈 DUAL_2PC 항목이 서버 설정 로드를 막지 않아야 함."""
    from server.config import Settings

    monkeypatch.setenv("DUAL_2PC_SUBJECT_INDEX", "")
    monkeypatch.setenv("DUAL_2PC_GROUP_ID", "")

    loaded = Settings(_env_file=None)

    assert loaded.dual_2pc_subject_index is None
    assert loaded.dual_2pc_group_id is None
