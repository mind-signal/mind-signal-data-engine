"""스트림 연속성 계측 검증 (.plans/22-stream-continuity-instrumentation P0-1).

배경: CSV 1행은 EEG 128샘플마다 생성되므로 결측은 행 단위로 통째 발생한다.
EEG가 15초 정지하면 DE watchdog 30초와 BE coverage 게이트 0.8을 둘 다 빠져나가,
다음 공백이 이벤트 유실인지 clock jump인지 판별할 수단이 없었다.

판정 로직은 헤드셋 없이 검증 가능하도록 순수 상태기로 분리했고, 여기서는
counter 누락과 timestamp 점프가 서로 다른 진단 코드로 기록되는지 잠근다.
"""

import json
import logging
import time
from types import SimpleNamespace

import pytest

from core.streamer import (
    COUNTER_MODULUS,
    MindSignalStreamer,
    StreamContinuityTracker,
    coerce_eeg_channels,
)

# 128Hz 정상 샘플 간격임
TICK = 1.0 / 128


def _feed(tracker, count, start_counter=0, start_time=1000.0):
    """정상 연속 샘플을 count개 주입하고 마지막 (counter, time) 반환함."""
    counter = start_counter
    now = start_time
    for _ in range(count):
        tracker.observe(counter, now)
        counter = (counter + 1) % COUNTER_MODULUS
        now += TICK
    return counter, now


def test_normal_stream_reports_nothing():
    tracker = StreamContinuityTracker()
    _feed(tracker, 300)
    assert tracker.lost_samples_lower_bound == 0
    assert tracker.events_seen == 300


def test_counter_gap_reports_missing_sample_count():
    tracker = StreamContinuityTracker()
    counter, now = _feed(tracker, 10)

    # 5샘플 유실 후 재개함
    anomalies = tracker.observe((counter + 5) % COUNTER_MODULUS, now + TICK)

    kinds = {a["kind"] for a in anomalies}
    assert kinds == {"counter_gap"}
    assert anomalies[0]["missingModulo"] == 5
    assert tracker.lost_samples_lower_bound == 5


def test_counter_gap_across_wrap_boundary():
    """127에서 2로 넘어가면 유실 2개임 (순환 경계에서 음수 gap 방지)."""
    tracker = StreamContinuityTracker()
    tracker.observe(127, 1000.0)
    anomalies = tracker.observe(2, 1000.0 + TICK)
    assert anomalies[0]["kind"] == "counter_gap"
    assert anomalies[0]["missingModulo"] == 2


def test_repeated_counter_is_distinct_from_gap():
    tracker = StreamContinuityTracker()
    tracker.observe(7, 1000.0)
    anomalies = tracker.observe(7, 1000.0 + TICK)
    assert [a["kind"] for a in anomalies] == ["counter_repeat"]
    assert tracker.lost_samples_lower_bound == 0


def test_clock_jump_signature_has_no_receive_gap():
    """이벤트는 정상 수신인데 Cortex 시각만 튄 경우임 — 스트림 정지가 아님."""
    tracker = StreamContinuityTracker()
    counter, now = _feed(tracker, 10)

    # _feed는 다음에 올 counter를 돌려주므로 그대로 넣으면 카운터는 연속임
    anomalies = tracker.observe(counter, now + 3.0, receive_gap_sec=TICK)

    kinds = {a["kind"] for a in anomalies}
    assert kinds == {"timestamp_jump", "full_cycle_loss_suspected"}
    jump = next(a for a in anomalies if a["kind"] == "timestamp_jump")
    assert jump["deltaSec"] == pytest.approx(3.0, abs=0.01)
    suspected = next(a for a in anomalies if a["kind"] == "full_cycle_loss_suspected")
    assert suspected["streamStopped"] is False


def test_stream_stop_signature_has_receive_gap():
    """15초 정지는 정확히 1920샘플이라 counter가 제자리로 돌아옴.

    codex 교차검토 지적(High): counter_gap만 보면 이 시나리오가 clock jump와
    구분되지 않는다. 로컬 수신 간격이 유일한 판별 신호다.
    """
    tracker = StreamContinuityTracker()
    counter, now = _feed(tracker, 10)

    # 15초 정지 후 복구 — 유실 1920 = 128 * 15라 counter는 연속으로 보임
    anomalies = tracker.observe(counter, now + 15.0, receive_gap_sec=15.0)

    kinds = {a["kind"] for a in anomalies}
    assert kinds == {"timestamp_jump", "receive_gap", "full_cycle_loss_suspected"}
    suspected = next(a for a in anomalies if a["kind"] == "full_cycle_loss_suspected")
    assert suspected["streamStopped"] is True


@pytest.mark.parametrize("lost", [127, 128, 133, 1920])
def test_full_cycle_losses_are_never_silently_normal(lost):
    """유실량이 modulus 배수든 아니든 어떤 이상은 반드시 남음."""
    tracker = StreamContinuityTracker()
    counter, now = _feed(tracker, 10)

    gap_sec = lost * TICK
    anomalies = tracker.observe(
        (counter + lost) % COUNTER_MODULUS, now + gap_sec, receive_gap_sec=gap_sec
    )

    assert anomalies, f"{lost}샘플 유실이 아무 이상도 남기지 않음"
    kinds = {a["kind"] for a in anomalies}
    # 어느 경우든 수신 공백이 남아 스트림 정지로 판별 가능함
    assert "receive_gap" in kinds
    if lost % COUNTER_MODULUS == 0:
        assert "counter_gap" not in kinds
        assert "full_cycle_loss_suspected" in kinds


def test_counter_gap_and_timestamp_jump_reported_together():
    """이벤트 유실이면 둘 다 뜸 — clock jump 단독과 구분되는 서명임."""
    tracker = StreamContinuityTracker()
    counter, now = _feed(tracker, 10)

    anomalies = tracker.observe((counter + 20) % COUNTER_MODULUS, now + 2.0)

    # counter_gap이 유실을 확정하므로 full_cycle_loss_suspected는 붙지 않음
    assert {a["kind"] for a in anomalies} == {"counter_gap", "timestamp_jump"}


def test_backwards_timestamp_is_distinct_kind():
    tracker = StreamContinuityTracker()
    counter, now = _feed(tracker, 5)
    anomalies = tracker.observe(counter, now - 1.0)
    assert [a["kind"] for a in anomalies] == ["timestamp_backwards"]


def test_non_finite_and_unparsable_values_do_not_raise():
    tracker = StreamContinuityTracker()
    assert [a["kind"] for a in tracker.observe(None, 1000.0)] == ["counter_invalid"]
    assert "time_invalid" in {a["kind"] for a in tracker.observe(1, float("nan"))}
    assert "time_invalid" in {a["kind"] for a in tracker.observe(2, "not-a-time")}


def test_interpolated_samples_counted():
    tracker = StreamContinuityTracker()
    tracker.observe(0, 1000.0, interpolated=1)
    tracker.observe(1, 1000.0 + TICK, interpolated=0)
    tracker.observe(2, 1000.0 + 2 * TICK, interpolated=1)
    assert tracker.summary()["interpolatedSamples"] == 2


def _bare_streamer():
    """Cortex와 Redis 부작용 없이 계측 경로만 돌리기 위한 인스턴스 생성함."""
    s = MindSignalStreamer.__new__(MindSignalStreamer)
    s.group_id = "g1"
    s.subject_index = 1
    s.start_time = time.time()
    s.last_data_time = time.time()
    s.last_met_time = time.time()
    s.eeg_channel_indices = []
    # 버퍼링 경로는 128샘플 미만이라 분석까지 가지 않음. 실제 analyzer 대신 fs만 둠
    s.eeg_buffer = []
    s.analyzer = SimpleNamespace(fs=128)
    s.counter_index = None
    s.interpolated_index = None
    s.continuity = StreamContinuityTracker()
    s.csv_rows_written = 0
    s.dropped_samples = 0
    s.dropped_blocks = 0
    s._continuity_log_counts = {}
    s._health_published_at = {}
    s._last_event_monotonic = None
    s._eeg_stale = False
    s._met_stale = False
    s.channel = "mind-signal:g1:subject:1"
    s.r = _FakeRedis()
    return s


class _FakeRedis:
    """publish 호출만 기록하는 대역임."""

    def __init__(self):
        self.published = []

    def publish(self, channel, message):
        self.published.append(json.loads(message))


def _continuity_records(caplog):
    return [
        json.loads(r.message.split("[CONTINUITY] ", 1)[1])
        for r in caplog.records
        if r.message.startswith("[CONTINUITY] ")
    ]


def test_labels_event_maps_counter_and_interpolated_indices():
    s = _bare_streamer()
    labels = ["COUNTER", "INTERPOLATED", "AF3", "T7", "Pz", "T8", "AF4"]
    s.on_new_data_labels(data={"streamName": "eeg", "labels": labels})
    assert s.counter_index == 0
    assert s.interpolated_index == 1
    assert s.eeg_channel_indices == [2, 3, 4, 5, 6]


def test_missing_counter_label_disables_instrumentation_without_breaking():
    s = _bare_streamer()
    s.on_new_data_labels(data={"streamName": "eeg", "labels": ["AF3", "AF4"]})
    assert s.counter_index is None
    # 계측이 꺼져도 EEG 수신 자체는 예외 없이 진행됨
    s.on_eeg_data_done(data={"eeg": [1.0, 2.0], "time": 1000.0})
    assert s.continuity.events_seen == 0


def test_eeg_event_logs_counter_gap(caplog):
    s = _bare_streamer()
    s.on_new_data_labels(
        data={"streamName": "eeg", "labels": ["COUNTER", "INTERPOLATED", "AF3"]}
    )
    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        s.on_eeg_data_done(data={"eeg": [10, 0, 1.0], "time": 1000.0})
        s.on_eeg_data_done(data={"eeg": [14, 0, 1.0], "time": 1000.0 + TICK})

    records = _continuity_records(caplog)
    assert [r["kind"] for r in records] == ["counter_gap"]
    assert records[0]["missingModulo"] == 3
    assert records[0]["subjectIndex"] == 1


def test_stale_start_and_recovery_are_edge_logged(caplog):
    s = _bare_streamer()
    s._eeg_stale = True  # watchdog이 정지를 이미 기록한 상태임
    s.last_data_time = time.time() - 15.0

    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        s.on_eeg_data_done(data={"eeg": [0, 0, 1.0], "time": 1000.0})
        # 복구 후 정상 수신은 다시 로그하지 않음
        s.on_eeg_data_done(data={"eeg": [1, 0, 1.0], "time": 1000.0 + TICK})

    records = _continuity_records(caplog)
    assert [r["kind"] for r in records] == ["eeg_recovered"]
    assert records[0]["silentSec"] >= 15.0
    assert s._eeg_stale is False


def test_log_cap_limits_flood_but_summary_keeps_full_count(caplog):
    s = _bare_streamer()
    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        for _ in range(50):
            s._log_continuity("counter_gap", {"missing": 1})

    records = _continuity_records(caplog)
    assert len(records) == 20
    assert records[-1]["logCapReached"] is True
    assert s._continuity_log_counts["counter_gap"] == 50


# --- P0-2 생성층 유한성 방어 ---


def test_coerce_accepts_normal_row():
    values, reason = coerce_eeg_channels([0, 0, 1.5, "2.5", 3], [2, 3, 4])
    assert reason is None
    assert values == [1.5, 2.5, 3.0]


@pytest.mark.parametrize(
    ("bad", "expected_reason"),
    [
        (float("nan"), "non_finite"),
        (float("inf"), "non_finite"),
        (float("-inf"), "non_finite"),
        ("not-a-number", "non_numeric"),
        (None, "non_numeric"),
    ],
)
def test_coerce_rejects_bad_values(bad, expected_reason):
    values, reason = coerce_eeg_channels([0, 0, 1.0, bad], [2, 3])
    assert values is None
    assert reason == expected_reason


def test_coerce_rejects_short_row_and_empty_mapping():
    assert coerce_eeg_channels([0, 1], [2, 3])[1] == "row_too_short"
    assert coerce_eeg_channels([0, 1], [])[1] == "no_channel_mapping"


def _mapped_streamer():
    s = _bare_streamer()
    s.on_new_data_labels(
        data={
            "streamName": "eeg",
            "labels": ["COUNTER", "INTERPOLATED", "AF3", "T7", "Pz", "T8", "AF4"],
        }
    )
    return s


def test_non_finite_sample_is_dropped_before_buffer(caplog):
    """Inf 샘플이 버퍼에 들어가면 128샘플 평균 전체가 오염됨."""
    s = _mapped_streamer()
    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        s.on_eeg_data_done(
            data={"eeg": [0, 0, 1.0, 1.0, float("inf"), 1.0, 1.0], "time": 1000.0}
        )

    assert s.eeg_buffer == []
    assert s.dropped_samples == 1
    records = _continuity_records(caplog)
    assert records[0]["kind"] == "eeg_sample_dropped"
    assert records[0]["reason"] == "non_finite"
    assert s.r.published[0]["status"] == "invalid_data"


def test_valid_sample_still_buffered():
    s = _mapped_streamer()
    s.on_eeg_data_done(data={"eeg": [0, 0, 1.0, 2.0, 3.0, 4.0, 5.0], "time": 1000.0})
    assert s.eeg_buffer == [[1.0, 2.0, 3.0, 4.0, 5.0]]
    assert s.dropped_samples == 0
    assert s.r.published == []


def test_health_republish_is_throttled():
    s = _mapped_streamer()
    for i in range(30):
        s.on_eeg_data_done(
            data={
                "eeg": [i % COUNTER_MODULUS, 0, float("nan"), 1.0, 1.0, 1.0, 1.0],
                "time": 1000.0 + i * TICK,
            }
        )
    assert s.dropped_samples == 30
    # 초당 128건을 던지지 않고 10초 간격으로 1건만 발행함
    assert len(s.r.published) == 1


def test_non_finite_power_block_is_not_written_to_csv(caplog):
    """샘플이 전부 유한해도 필터 overflow로 한 대역만 비유한일 수 있음."""
    s = _mapped_streamer()
    s.analyzer = SimpleNamespace(
        fs=2,
        get_all_powers=lambda _: {
            "delta": 1.0,
            "theta": float("inf"),
            "alpha": 1.0,
            "beta": 1.0,
            "gamma": 1.0,
        },
    )
    s.writer = SimpleNamespace(
        writerow=lambda row: pytest.fail("불량 블록이 CSV에 기록됨")
    )

    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        s.on_eeg_data_done(
            data={"eeg": [0, 0, 1.0, 1.0, 1.0, 1.0, 1.0], "time": 1000.0}
        )
        s.on_eeg_data_done(
            data={"eeg": [1, 0, 1.0, 1.0, 1.0, 1.0, 1.0], "time": 1001.0}
        )

    assert s.dropped_blocks == 1
    assert s.csv_rows_written == 0
    assert s.eeg_buffer == []  # 다음 초가 오염되지 않도록 버퍼 초기화됨
    dropped = [
        r for r in _continuity_records(caplog) if r["kind"] == "eeg_block_dropped"
    ]
    assert dropped[0]["bands"] == ["theta"]
    assert s.r.published[0]["status"] == "invalid_data"


# --- codex 교차검토 반영분 ---


def test_bad_sample_discards_partial_buffer(caplog):
    """불량 샘플 전후가 한 블록으로 이어붙지 않음 (codex Medium 2)."""
    s = _mapped_streamer()
    s.on_eeg_data_done(data={"eeg": [0, 0, 1.0, 1.0, 1.0, 1.0, 1.0], "time": 1000.0})
    assert len(s.eeg_buffer) == 1

    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        s.on_eeg_data_done(
            data={"eeg": [1, 0, float("nan"), 1.0, 1.0, 1.0, 1.0], "time": 1000.01}
        )

    assert s.eeg_buffer == []
    discarded = [
        r
        for r in _continuity_records(caplog)
        if r["kind"] == "partial_buffer_discarded"
    ]
    assert discarded[0]["samples"] == 1


def test_receive_gap_discards_partial_buffer(monkeypatch):
    """수신이 끊겼다 재개되면 떨어진 두 구간을 이어붙이지 않음."""
    s = _mapped_streamer()
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    s.on_eeg_data_done(data={"eeg": [0, 0, 1.0, 1.0, 1.0, 1.0, 1.0], "time": 1000.0})
    s.on_eeg_data_done(data={"eeg": [1, 0, 1.0, 1.0, 1.0, 1.0, 1.0], "time": 1000.01})
    assert len(s.eeg_buffer) == 2

    clock[0] += 15.0  # 15초 수신 공백 후 복구됨
    s.on_eeg_data_done(data={"eeg": [2, 0, 1.0, 1.0, 1.0, 1.0, 1.0], "time": 1015.0})

    # 공백 이전 2샘플은 버려지고 새 구간만 남음
    assert len(s.eeg_buffer) == 1


def test_non_finite_time_does_not_kill_callback(caplog):
    """비유한 cortex_time은 fromtimestamp에서 콜백을 죽임 (codex Medium 3)."""
    s = _mapped_streamer()
    s.analyzer = SimpleNamespace(
        fs=1,
        get_all_powers=lambda _: {
            "delta": 1.0,
            "theta": 1.0,
            "alpha": 1.0,
            "beta": 1.0,
            "gamma": 1.0,
        },
    )
    s.writer = SimpleNamespace(
        writerow=lambda row: pytest.fail("비유한 시각 행이 CSV에 기록됨")
    )

    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        s.on_eeg_data_done(
            data={"eeg": [0, 0, 1.0, 1.0, 1.0, 1.0, 1.0], "time": float("nan")}
        )

    assert s.dropped_blocks == 1
    assert s.csv_rows_written == 0
    dropped = [
        r for r in _continuity_records(caplog) if r["kind"] == "eeg_block_dropped"
    ]
    assert dropped[0]["reason"] == "non_finite_time"


def test_non_finite_met_keeps_last_good_value(caplog):
    """NaN MET을 그대로 저장하면 CSV와 Redis payload가 오염됨 (codex Medium 3)."""
    s = _mapped_streamer()
    s.met_map = {"focus": 0, "stress": 1}
    s.latest_met = {"focus": 0.4, "stress": 0.2}

    with caplog.at_level(logging.WARNING, logger="core.streamer"):
        s.on_new_met_data(data={"met": [float("nan"), 0.9]})

    assert s.latest_met == {"focus": 0.4, "stress": 0.9}
    rejected = [
        r for r in _continuity_records(caplog) if r["kind"] == "met_value_rejected"
    ]
    assert rejected[0]["metric"] == "focus"
