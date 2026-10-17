"""재생 드라이버(scripts/replay_driver.py)의 판정 계산 테스트 (OPS-W019)."""

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

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


def _pairs(both: int, single: int) -> list[dict]:
    """양쪽 쌍 both개와 한쪽 쌍 single개로 된 aligned_pair 목록을 만듦."""
    return [{"subject_1": WAVES, "subject_2": WAVES}] * both + [
        {"subject_1": WAVES, "subject_2": None}
    ] * single


def _judge(**over: Any) -> dict:
    """모든 항목이 통과하는 기본값으로 judge를 호출함. over로 인자를 바꿈."""
    kwargs = dict(
        pairs=_pairs(190, 5),
        rows={1: 200, 2: 198},
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
    assert v["aligned_pairs"]["both"] == 190
    assert v["aligned_pairs"]["ratio"] == round(190 / 198, 4)
    assert v["single_emit"]["single"] == 5


def test_paired_ratio_below_threshold_fails():
    """양쪽 쌍 비율이 기준 미만이면 실패하는지 확인함."""
    v = _judge(pairs=_pairs(170, 0))
    assert not v["aligned_pairs"]["pass"]
    assert not v["pass"]


def test_single_emit_ratio_over_threshold_fails():
    """단독 emit 비율이 기준 초과면 실패하는지 확인함."""
    v = _judge(pairs=_pairs(190, 30))
    assert v["aligned_pairs"]["pass"]
    assert not v["single_emit"]["pass"]


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
