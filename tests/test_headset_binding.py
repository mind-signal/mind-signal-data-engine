"""동적 헤드셋 바인딩 재현/회귀 테스트.

문제 1: core/main.py가 HEADSET_ID_{idx} 환경변수로 헤드셋을 고정 바인딩한다.
원하는 동작: env를 무시하고 빈 문자열을 넘겨 SDK가 첫 연결 헤드셋을 자동 선택한다
(어떤 헤드셋이 붙어도 그 PC의 subject로 값 주입).

subject_index 1/2를 모두 파라미터화해 양쪽 HEADSET_ID env가 전부 무시되는지 잠근다.
"""

import sys

import pytest

import core.main as main_module
from core.streamer import MindSignalStreamer
from sdk.cortex import Cortex


@pytest.mark.parametrize(
    ("subject_index", "env_var"),
    [("1", "HEADSET_ID_1"), ("2", "HEADSET_ID_2")],
)
def test_main_ignores_headset_id_env_and_lets_sdk_pick_first(
    monkeypatch, subject_index, env_var
):
    captured = {}

    class FakeStreamer:
        def __init__(
            self,
            group_id,
            subject_index,
            client_id,
            client_secret,
            headset_id="",
            **kwargs,
        ):
            captured["headset_id"] = headset_id

        def open(self):
            captured["opened"] = True

    monkeypatch.setattr(main_module, "MindSignalStreamer", FakeStreamer)
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    # 하드코딩 시나리오: 해당 subject의 헤드셋 ID가 env에 박혀 있어도
    monkeypatch.setenv(env_var, "INSIGHT2-HARDCODED")
    monkeypatch.setattr(sys, "argv", ["core.main", "group-xyz", subject_index])

    main_module.main()

    # 동적 바인딩: env의 하드코딩 ID를 절대 통과시키지 않고 빈 문자열을 넘겨야 한다.
    assert captured["headset_id"] == ""


def _make_streamer():
    """Cortex.__init__ 부작용 없이 _handle_query_headset만 테스트하기 위한 인스턴스 생성함"""
    s = MindSignalStreamer.__new__(MindSignalStreamer)
    s.headset_id = ""
    s.subject_index = 2
    return s


def _capture_super(monkeypatch):
    """super()._handle_query_headset 호출 시점의 headset_id를 캡처함"""
    captured = {}
    monkeypatch.setattr(
        Cortex,
        "_handle_query_headset",
        lambda self, rd: captured.update(headset_id=self.headset_id),
    )
    return captured


def test_query_headset_prefers_single_connected(monkeypatch):
    """discovered가 먼저여도 connected 헤드셋을 우선 지정함 (2026-07-02 subscribe 실패 회귀)."""
    s = _make_streamer()
    captured = _capture_super(monkeypatch)
    result = [
        {"id": "8E9", "status": "discovered", "connectedBy": "bluetooth"},
        {"id": "5B", "status": "connected", "connectedBy": "bluetooth"},
    ]
    s._handle_query_headset(result)
    assert s.headset_id == "5B"
    assert captured["headset_id"] == "5B"


def test_query_headset_skips_when_multiple_connected(monkeypatch):
    """connected 2대 이상이면 오선택 방지로 자동 우선선택 skip하고 SDK 위임함."""
    s = _make_streamer()
    captured = _capture_super(monkeypatch)
    result = [
        {"id": "A", "status": "connected", "connectedBy": "bluetooth"},
        {"id": "B", "status": "connected", "connectedBy": "bluetooth"},
    ]
    s._handle_query_headset(result)
    assert s.headset_id == ""
    assert captured["headset_id"] == ""


def test_query_headset_no_connected_delegates(monkeypatch):
    """connected가 없으면 headset_id 미지정 상태로 SDK 기본 폴백에 위임함 (#26 보존)."""
    s = _make_streamer()
    captured = _capture_super(monkeypatch)
    result = [{"id": "8E9", "status": "discovered", "connectedBy": "bluetooth"}]
    s._handle_query_headset(result)
    assert s.headset_id == ""
    assert captured["headset_id"] == ""
