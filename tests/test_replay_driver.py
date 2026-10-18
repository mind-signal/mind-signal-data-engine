"""재생 드라이버(scripts/replay_driver.py)의 판정 계산 테스트 (OPS-W019)."""

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "replay_driver.py"
GROUP_ID = "6a72db3c2d01f2ce5c690b19"
WAVES = {"waves": {"delta": 1.0}}


def _load() -> Any:
    """scripts/ 는 패키지가 아니라 파일 경로로 직접 로드함."""
    spec = importlib.util.spec_from_file_location("replay_driver", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_driver"] = module
    spec.loader.exec_module(module)
    return module


drv = _load()


def _pairs(both: int, single: int, single_subject: int = 1) -> list[dict]:
    """양쪽 쌍 both개와 single_subject 한쪽만 있는 쌍 single개로 된 목록을 만듦."""
    other = 3 - single_subject
    return [{"subject_1": WAVES, "subject_2": WAVES}] * both + [
        {f"subject_{single_subject}": WAVES, f"subject_{other}": None}
    ] * single


def _judge(**over: Any) -> dict:
    """모든 항목이 통과하는 기본값으로 judge를 호출함. over로 인자를 바꿈."""
    kwargs = dict(
        pairs=_pairs(200, 0),
        rows={1: 200, 2: 200},
        complete_events=1,
        session_statuses=["COMPLETED", "COMPLETED"],
        analysis={"isBTI": False, "matchingScore": 70},
        analysis_status_events=[],
    )
    kwargs.update(over)
    return drv.judge(**kwargs)


def test_all_pass():
    """기본 입력에서 판정 4개가 모두 통과하는지 확인함."""
    v = _judge()
    assert v["pass"]
    assert v["aligned_pairs"]["both"] == 200
    assert v["aligned_pairs"]["ratio"] == 1.0
    assert v["single_emit"]["single"] == 0
    assert v["accounting"]["subjects"]["1"] == {
        "sent": 200,
        "paired": 200,
        "single": 0,
        "loss": 0,
    }


def test_paired_ratio_below_threshold_fails():
    """양쪽 쌍 비율이 기준 미만이면 실패하는지 확인함."""
    v = _judge(pairs=_pairs(170, 0))
    assert not v["aligned_pairs"]["pass"]
    assert not v["pass"]


def test_single_emit_ratio_over_threshold_fails():
    """단독 emit 비율이 기준 초과면 실패하는지 확인함."""
    pairs = _pairs(196, 4) + _pairs(0, 4, single_subject=2)
    v = _judge(pairs=pairs, rows={1: 200, 2: 200})
    assert v["aligned_pairs"]["pass"]
    assert v["accounting"]["pass"]
    assert not v["single_emit"]["pass"]


def test_pair_count_over_sent_rows_fails():
    """정렬 쌍이 어느 subject의 보낸 행보다 많으면(중복 쌍) 실패하는지 확인함."""
    v = _judge(pairs=_pairs(205, 0), rows={1: 200, 2: 205})
    assert not v["aligned_pairs"]["pass"]
    assert v["accounting"]["subjects"]["1"]["loss"] == -5
    assert not v["accounting"]["pass"]
    assert not v["pass"]


def test_one_sided_loss_is_not_hidden_by_min_rows():
    """한쪽 subject만 행을 잃으면 분모 min(rows)로 가려지지 않고 실패하는지 확인함."""
    v = _judge(pairs=_pairs(180, 0), rows={1: 202, 2: 180})
    assert v["accounting"]["subjects"]["1"]["loss"] == 22
    assert not v["accounting"]["pass"]
    assert not v["aligned_pairs"]["pass"]
    assert not v["pass"]


@pytest.mark.parametrize("run", ["OPS-W019 run3", "EEG-W007 t5 1차"])
def test_pre_fix_resend_run_fails(run: str):
    """수정 전 결함 결과(쌍 201, 단독 20/221, 행 202/201)가 실패하는지 확인함.

    두 결과 JSON의 집계값만 옮김. 단독 20건은 재전송한 엔진 2의 행임(PLAN 6.5a).
    """
    v = _judge(pairs=_pairs(201, 20, single_subject=2), rows={1: 202, 2: 201})
    assert not v["single_emit"]["pass"], run
    assert v["accounting"]["subjects"]["1"]["loss"] == 1
    assert v["accounting"]["subjects"]["2"]["loss"] == -20
    assert not v["accounting"]["pass"]
    assert not v["pass"]


def test_expected_after_eeg_w008_passes():
    """EEG-W008 반영 뒤 기대값(쌍 202/202, 단독 0, 손실 0)이 통과하는지 확인함."""
    v = _judge(pairs=_pairs(202, 0), rows={1: 202, 2: 202})
    assert v["pass"]
    for s in ("1", "2"):
        assert v["accounting"]["subjects"][s]["loss"] == 0


def test_stop_flush_single_of_longer_subject_passes():
    """한쪽이 1행 덜 보내 남은 행이 정지 flush로 단독 emit되면 손실 0으로 통과함."""
    v = _judge(pairs=_pairs(201, 1, single_subject=1), rows={1: 202, 2: 201})
    assert v["accounting"]["subjects"]["1"] == {
        "sent": 202,
        "paired": 201,
        "single": 1,
        "loss": 0,
    }
    assert v["pass"]


def test_read_harness_counts_sums_backend_windows_and_takes_last_relay(
    tmp_path: Path,
):
    """백엔드 창별 요약은 더하고 중계기 누적 요약은 마지막 줄을 쓰는지 확인함."""
    (tmp_path / "backend.log").write_text(
        "[/proxy] summary subject=2 received=11 published=10 duplicate=1 "
        "in_flight=0 invalid=0 channels=[c]\n"
        "other line\n"
        "[/proxy] summary subject=1 received=10 published=10 duplicate=0 "
        "in_flight=0 invalid=0 channels=[c]\n"
        "[/proxy] summary subject=2 received=13 published=10 duplicate=1 "
        "in_flight=2 invalid=0 channels=[c]\n"
    )
    (tmp_path / "relay.log").write_text(
        "[relay] :5101 → http://127.0.0.1:5100 (지연 없음)\n"
        "[relay] 전달 10, 지연 0, 중복 1\n"
        "[relay] 전달 40, 지연 5, 중복 4\n",
        encoding="utf-8",
    )
    c = drv.read_harness_counts(tmp_path)
    assert c["backend"] == {
        "1": {"duplicate": 0, "in_flight": 0},
        "2": {"duplicate": 2, "in_flight": 2},
    }
    assert c["relay"] == {"forwarded": 40, "delayed": 5, "duplicated": 4}
    assert "backend_reason" not in c and "relay_reason" not in c


def test_read_harness_counts_null_with_reason(tmp_path: Path):
    """로그 폴더가 없거나 로그 파일이 없으면 null과 이유를 남기는지 확인함."""
    none = drv.read_harness_counts(None)
    assert none["backend"] is None and none["relay"] is None
    assert none["backend_reason"] and none["relay_reason"]

    missing = drv.read_harness_counts(tmp_path)
    assert missing["backend"] is None and "backend.log" in missing["backend_reason"]
    assert missing["relay"] is None and "relay.log" in missing["relay_reason"]


def test_read_harness_counts_unreadable_log_keeps_verdict(tmp_path: Path, monkeypatch):
    """로그를 읽다 OSError가 나도 예외 없이 null과 이유를 남기는지 확인함."""
    (tmp_path / "backend.log").write_text("x\n")
    (tmp_path / "relay.log").write_text("x\n")

    def deny(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", deny)
    c = drv.read_harness_counts(tmp_path)
    assert c["backend"] is None and "읽기 실패" in c["backend_reason"]
    assert c["relay"] is None and "읽기 실패" in c["relay_reason"]


def test_read_harness_counts_is_file_oserror_keeps_verdict(tmp_path: Path, monkeypatch):
    """Python 3.10의 is_file()이 권한 문제로 OSError를 내도 null과 이유를 남기는지 확인함."""

    def deny(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", deny)
    c = drv.read_harness_counts(tmp_path)
    assert c["backend"] is None and "읽기 실패" in c["backend_reason"]
    assert c["relay"] is None and "읽기 실패" in c["relay_reason"]


def test_no_pairs_fails_both_pair_checks():
    """aligned_pair가 없으면 쌍 판정 둘 다 실패하는지 확인함."""
    v = _judge(pairs=[], rows={1: 0, 2: 0})
    assert not v["aligned_pairs"]["pass"]
    assert not v["single_emit"]["pass"]


def test_measurement_complete_needs_one_event_and_two_completed():
    """측정 종료 판정이 이벤트 1회와 두 세션 COMPLETED를 요구하는지 확인함."""
    assert not _judge(complete_events=2)["measurement_complete"]["pass"]
    assert not _judge(complete_events=0)["measurement_complete"]["pass"]
    statuses = ["COMPLETED", "CANCELLED"]
    assert not _judge(session_statuses=statuses)["measurement_complete"]["pass"]


def test_analysis_fails_without_result_or_with_tier_event():
    """분석 결과 없음, BTI 결과, 등급 이벤트 수신이면 분석 판정이 실패하는지 확인함."""
    assert not _judge(analysis=None)["analysis"]["pass"]
    assert not _judge(analysis={"isBTI": True})["analysis"]["pass"]
    partial = [{"tier": "PARTIAL"}]
    assert not _judge(analysis_status_events=partial)["analysis"]["pass"]


def test_count_csv_rows_uses_newest_file(tmp_path: Path):
    """행 수 집계가 가장 최근 파일을 쓰고 파일이 없으면 0인지 확인함."""
    old = tmp_path / f"subject_1_{GROUP_ID}_20261018_100000.csv"
    new = tmp_path / f"subject_1_{GROUP_ID}_20261018_100500.csv"
    old.write_text("h\n1\n")
    new.write_text("h\n1\n2\n3\n")

    os.utime(old, (1, 1))
    assert drv.count_csv_rows(tmp_path, GROUP_ID, 1) == 3
    assert drv.count_csv_rows(tmp_path, GROUP_ID, 2) == 0


class _FakeWs:
    """recv 순서를 미리 정한 가짜 웹소켓임."""

    def __init__(self, frames: list[str]) -> None:
        """recv가 차례로 돌려줄 프레임 목록을 받음."""
        self.frames = frames
        self.sent: list[str] = []

    def recv(self) -> str:
        """다음 프레임을 반환함. 없으면 연결 끊김으로 OSError 발생함."""
        if not self.frames:
            raise OSError("closed")
        return self.frames.pop(0)

    def send(self, msg: str) -> None:
        """보낸 프레임을 기록함."""
        self.sent.append(msg)


def test_socket_recorder_parses_events_acks_and_pings():
    """Socket.io 프레임에서 이벤트, 응답, ping 처리가 맞는지 확인함."""
    rec = drv.SocketRecorder("http://127.0.0.1:5100")
    assert rec._url == "ws://127.0.0.1:5100/socket.io/?EIO=4&transport=websocket"
    rec._ws = _FakeWs(
        [
            "2",
            '430[{"ok":true,"groupId":"g"}]',
            '42["aligned_pair",{"subject_1":null}]',
            '42["measurement-complete",{"status":"COMPLETED"}]',
        ]
    )
    rec._loop()
    assert rec._ws.sent == ["3"]
    assert rec.acks == [[{"ok": True, "groupId": "g"}]]
    assert rec.named("aligned_pair") == [{"subject_1": None}]
    assert rec.named("measurement-complete") == [{"status": "COMPLETED"}]


def test_natural_adds_stop_reason_and_tier_event():
    """natural이면 stop_reason과 tier_event가 판정에 들어가는지 확인함."""
    ok = _judge(
        natural=True,
        stop_reasons=["Natural", "Natural"],
        analysis_status_events=[{"tier": "VALID"}],
    )
    assert ok["pass"] and ok["analysis"]["pass"]
    assert ok["tier_event"]["tiers"] == ["VALID"]

    before_w008 = _judge(natural=True, stop_reasons=[None, None])
    assert not before_w008["stop_reason"]["pass"]
    assert not before_w008["tier_event"]["pass"]
    assert not before_w008["pass"]

    manual = _judge(natural=True, stop_reasons=["Natural", "ManualEarly"])
    assert not manual["stop_reason"]["pass"]


def test_non_natural_ignores_natural_items():
    """natural이 아니면 자연 종료 항목을 넣지 않는지 확인함."""
    v = _judge(stop_reasons=[None, None])
    assert v["pass"]
    assert "stop_reason" not in v and "tier_event" not in v


class _FakeDriver:
    """stop-all만 실패하는 백엔드 대역. stop_error에 던질 예외를 넣음."""

    stop_error: Exception = RuntimeError("POST stop-all -> 500")

    def __init__(self, backend_url: str) -> None:
        """호출 경로를 기록할 목록을 만듦."""
        self.paths: list[str] = []

    def user(self, email: str, password: str, name: str) -> str:
        """가입 없이 토큰을 돌려줌."""
        return "token"

    def call(self, method: str, path: str, token: str | None = None, **kw) -> dict:
        """경로별 최소 응답을 돌려주고 stop-all은 2xx 아님으로 실패시킴."""
        self.paths.append(path)
        if path.endswith("stop-all"):
            raise self.stop_error
        if path == "/api/engine/registry-status":
            return {"ready": True}
        if path.startswith("/api/sessions/group/"):
            return {"data": {"sessions": [{"status": "MEASURING"}] * 2}}
        return {"data": {"groupId": GROUP_ID, "pairingToken": "p", "token": "t"}}


class _FakeSocket:
    """join-room ack와 dual-session-ready만 주는 소켓 대역."""

    acks = [({"ok": True},)]

    def __init__(self, backend_url: str) -> None:
        """인자는 쓰지 않음."""

    def connect(self) -> None:
        """연결하지 않음."""

    def emit(self, event: str, data: object, ack_id: int | None = None) -> None:
        """보내지 않음."""

    def named(self, name: str) -> list:
        """측정 시작 이벤트만 있음. measurement-complete는 오지 않음."""
        return [{}] if name == "dual-session-ready" else []

    def close(self) -> None:
        """닫을 것이 없음."""


@pytest.mark.parametrize(
    "error",
    [RuntimeError("POST stop-all -> 500"), httpx.ConnectError("connection refused")],
)
def test_natural_timeout_keeps_verdict_when_stop_all_fails(
    tmp_path, monkeypatch, error
):
    """자연 종료 시간 초과 뒤 stop-all이 실패해도 groupId와 실패 판정이 남음."""
    wait = drv._wait
    monkeypatch.setattr(drv, "_wait", lambda p, t, i=1.0: wait(p, min(t, 0.05), 0.01))
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    monkeypatch.setattr(_FakeDriver, "stop_error", error)
    monkeypatch.setattr(drv, "Driver", _FakeDriver)
    monkeypatch.setattr(drv, "SocketRecorder", _FakeSocket)
    args = drv._parse_args(["--csv-dir", str(tmp_path), "--natural"])

    result = drv.run(args)

    assert result["groupId"] == GROUP_ID
    assert result["verdict"]["measurement_complete"]["pass"] is False
    assert result["verdict"]["pass"] is False
