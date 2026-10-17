"""_start_proxy_health_monitor 데몬 스레드 생존성 테스트.

이 스레드는 proxy /health 를 폴링해 fail-closed 를 발동시키는 안전장치다.
루프 안에서 예상 밖 예외가 하나라도 새어나가면 스레드가 아무 흔적 없이 죽고,
프록시가 죽어도 측정이 계속된다 — fail-closed 가 막으려던 바로 그 상황이다.
"""

import threading
import time
from typing import Any

import core.streamer as streamer_module


class _FakeStreamer:
    """_start_proxy_health_monitor 만 떼어내 돌리기 위한 최소 대역임.

    실제 MindSignalStreamer 는 Cortex 를 상속해 생성자가 헤드셋 연결을 요구하므로
    그대로 인스턴스화할 수 없음. 메서드만 빌려와 필요한 속성을 직접 채움.
    """

    proxy_url = "http://unused.invalid"
    subject_index = 1
    proxy_health_poll_interval_sec = 0.01
    proxy_fail_closed_threshold_ms = 3000

    def __init__(self) -> None:
        self._watchdog_active = True
        self._fail_closed_triggered = False
        self.closed = False

    def close_session(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    _start_proxy_health_monitor = (
        streamer_module.MindSignalStreamer._start_proxy_health_monitor
    )


def _run_monitor_with(monkeypatch: Any, side_effect: Any) -> _FakeStreamer:
    """check_health 를 side_effect 로 바꿔치기하고 감시 스레드를 잠시 돌림."""
    monkeypatch.setattr(streamer_module, "check_health", side_effect)
    fake = _FakeStreamer()
    fake._start_proxy_health_monitor()
    time.sleep(0.15)
    return fake


def test_monitor_survives_unexpected_exception_from_check_health(monkeypatch) -> None:
    """check_health 가 RequestError 계열이 아닌 예외를 던져도 스레드가 살아 있어야 함.

    회귀 재현 — 루프에 예외 wrap 이 없으면 첫 예외에서 스레드가 즉시 죽는다.
    """
    calls = {"n": 0}

    def boom(_url: str) -> bool:
        calls["n"] += 1
        raise RuntimeError("예상 밖 예외임")

    fake = _run_monitor_with(monkeypatch, boom)

    # 스레드가 죽었으면 호출이 1회에서 멈춘다. 살아 있으면 계속 폴링함
    assert calls["n"] > 1, f"감시 스레드가 첫 예외에서 죽음 (호출 {calls['n']}회)"

    fake._watchdog_active = False
    time.sleep(0.05)


def test_monitor_still_trips_fail_closed_after_transient_exception(
    monkeypatch,
) -> None:
    """예외가 한 번 난 뒤에도 이후 unhealthy 누적으로 fail-closed 가 발동해야 함."""
    state = {"n": 0}

    def flaky(_url: str) -> bool:
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("첫 폴링만 예외임")
        return False  # 이후 계속 unhealthy

    fake = _FakeStreamer()
    fake.proxy_fail_closed_threshold_ms = 20  # 빠르게 발동시킴
    monkeypatch.setattr(streamer_module, "check_health", flaky)
    fake._start_proxy_health_monitor()
    time.sleep(0.3)

    assert fake._fail_closed_triggered is True
    assert fake.closed is True

    fake._watchdog_active = False


def test_monitor_thread_is_daemon(monkeypatch) -> None:
    """감시 스레드는 daemon 이어야 프로세스 종료를 막지 않음."""
    before = {t.ident for t in threading.enumerate()}
    fake = _run_monitor_with(monkeypatch, lambda _url: True)
    new = [t for t in threading.enumerate() if t.ident not in before]

    assert new, "감시 스레드가 생성되지 않음"
    assert all(t.daemon for t in new)

    fake._watchdog_active = False
    time.sleep(0.05)
