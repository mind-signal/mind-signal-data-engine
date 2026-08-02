import csv
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from core.analyzer import MindSignalAnalyzer
from sdk.cortex import Cortex
from server.config import settings
from server.services.proxy_client import (
    ProxyForwardError,
    ProxyHealthTracker,
    check_health,
    post_sample,
)
from server.services.webhook import upload_csv_to_backend

logger = logging.getLogger(__name__)

# Cortex met 스트림의 지표별 라벨 후보임. 헤드셋 세대마다 라벨명이 달라
# 후보를 순서대로 탐색해 처음 발견된 것을 채택함.
# Insight2는 focus를 'attention'으로 보냄 (2026-07-10 라이브 로그 실측).
# 기존 코드가 'foc'만 찾아 focus가 전 구간 0으로 기록되던 결함을 수정함.
MET_LABEL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "focus": ("attention", "foc"),
    "engagement": ("eng",),
    "interest": ("int",),
    "excitement": ("exc",),
    "stress": ("str",),
    "relaxation": ("rel",),
}


# Cortex EEG COUNTER는 0부터 127까지 순환하는 샘플 카운터임 (128Hz라 1초에 한 바퀴).
COUNTER_MODULUS = 128
# 128Hz 기준 정상 샘플 간격은 약 0.0078초임. 이보다 크게 벌어지면 이벤트 유실이나
# clock jump로 판정함. 0.5초는 CSV 1행(1초) 절반이라 행 단위 결측 직전에 잡힘.
TIMESTAMP_JUMP_TOLERANCE_SEC = 0.5
# 같은 종류의 이상을 무한히 찍지 않기 위한 상한임. 초과분은 개수만 세어 종료 요약에 남김.
# 주의: 이 상한은 개별 이벤트에만 걸리고 stream_rollup에는 걸리지 않음. 상한만
# 두면 초반에 소진돼 이후 구간이 통째로 무기록이 됨(2026-07-31 실측: receive_gap
# 20건이 4.1초에서 29.1초 사이에 소진, 이후 6분 무기록).
CONTINUITY_LOG_CAP_PER_KIND = 20
# 롤업 주기임. 상한 없이 전 구간의 추이를 남기는 유일한 경로라 너무 길면
# 사건 발생 구간을 좁히지 못하고, 너무 짧으면 로그가 불필요하게 늘어남.
# 10분 측정 기준 20줄이라 부담이 없음.
CONTINUITY_ROLLUP_INTERVAL_SEC = 30
# 매 샘플 발생 가능한 경보의 재발행 최소 간격임. BE도 같은 status 연속 수신을
# 억제하지만(stream-health.service.ts), Redis에 초당 128건을 던질 이유가 없음.
HEALTH_REPUBLISH_INTERVAL_SEC = 10
# Emotiv Insight의 뇌파 채널임. CSV 채널별 컬럼 순서의 정본이며, 실제 헤드셋에
# 일부 채널이 없어도 헤더는 이 순서 그대로 고정함(스키마 안정성).
TARGET_EEG_CHANNELS = ["AF3", "T7", "Pz", "T8", "AF4"]
# 대역별 컬럼 순서의 정본임. analyzer.BANDS와 같은 순서를 유지함.
BAND_NAMES = ["delta", "theta", "alpha", "beta", "gamma"]


class StreamContinuityTracker:
    """Cortex EEG 이벤트의 연속성 이상을 판정하는 순수 상태기임.

    헤드셋 없이 검증 가능하도록 I/O를 하지 않고 값만 받아 이상 목록을 반환함.
    호출부(`MindSignalStreamer.on_eeg_data_done`)가 로그 기록을 담당함.

    판별표 (세 신호의 조합으로 원인을 가름):

    | receive_gap | timestamp_jump | counter_gap | 해석 |
    |---|---|---|---|
    | 큼 | 큼 | 있거나 없음 | 스트림 정지 — 이벤트가 실제로 안 옴 |
    | 정상 | 큼 | 없음 | clock jump — 이벤트는 왔고 시각만 튐 |
    | 정상 | 정상 | 있음 | 국소 유실 — modulus 미만 샘플 누락 |

    COUNTER는 modulus 순환이라 정확히 128의 배수만큼 유실되면 제자리로 돌아와
    counter_gap으로 안 잡힘. 128Hz에서 "N초 정지"는 언제나 N*128 샘플이라 이 경우가
    오히려 대표 시나리오임. 그래서 counter 하나에 의존하지 않고 로컬 수신 간격
    (receive_gap_sec)을 항상 함께 받아 판별함. counter가 연속인데 수신 간격이나
    Cortex 시각이 벌어졌으면 full_cycle_loss_suspected로 남김.
    """

    def __init__(
        self,
        modulus: int = COUNTER_MODULUS,
        jump_tolerance_sec: float = TIMESTAMP_JUMP_TOLERANCE_SEC,
    ):
        self.modulus = modulus
        self.jump_tolerance_sec = jump_tolerance_sec
        self.prev_counter: int | None = None
        self.prev_time: float | None = None
        self.events_seen = 0
        # modulus 순환 때문에 확정 유실량이 아니라 하한임 (이름으로 못 박음)
        self.lost_samples_lower_bound = 0
        self.interpolated_samples = 0

    def observe(
        self,
        counter: object,
        cortex_time: object,
        interpolated: object = 0,
        receive_gap_sec: float | None = None,
    ) -> list[dict]:
        """EEG 이벤트 1건을 관측하고 발견된 이상 목록 반환함.

        Args:
            counter - Cortex EEG COUNTER 값임 (0부터 modulus-1까지 순환)
            cortex_time - Cortex가 보낸 epoch 초임
            interpolated - Cortex INTERPOLATED 플래그임 (1이면 보간 샘플)
            receive_gap_sec - 직전 이벤트와의 로컬 수신 간격임 (monotonic 기준).
                None이면 수신 간격 판정을 건너뜀

        Returns:
            이상 dict 리스트 반환. 정상이면 빈 리스트임. 각 dict는 `kind` 키를
            가지며 종류별로 부가 필드가 붙음.
        """
        self.events_seen += 1
        anomalies: list[dict] = []
        counter_gapped = False

        try:
            counter_int = int(counter)
        except (TypeError, ValueError, OverflowError):
            anomalies.append({"kind": "counter_invalid", "value": repr(counter)})
            counter_int = None

        if counter_int is not None:
            if self.prev_counter is not None:
                if counter_int == self.prev_counter:
                    anomalies.append({"kind": "counter_repeat", "counter": counter_int})
                else:
                    missing = (counter_int - self.prev_counter - 1) % self.modulus
                    if missing:
                        counter_gapped = True
                        self.lost_samples_lower_bound += missing
                        anomalies.append(
                            {
                                "kind": "counter_gap",
                                "prev": self.prev_counter,
                                "counter": counter_int,
                                # 실제 유실량이 아니라 유실량 mod modulus임
                                "missingModulo": missing,
                            }
                        )
            self.prev_counter = counter_int

        try:
            time_float = float(cortex_time)
        except (TypeError, ValueError, OverflowError):
            time_float = float("nan")
        time_jumped = False
        if not np.isfinite(time_float):
            anomalies.append({"kind": "time_invalid", "value": repr(cortex_time)})
        else:
            if self.prev_time is not None:
                delta = time_float - self.prev_time
                if delta < 0:
                    anomalies.append(
                        {"kind": "timestamp_backwards", "deltaSec": round(delta, 4)}
                    )
                elif delta > self.jump_tolerance_sec:
                    time_jumped = True
                    anomalies.append(
                        {"kind": "timestamp_jump", "deltaSec": round(delta, 4)}
                    )
            self.prev_time = time_float

        receive_gapped = (
            receive_gap_sec is not None and receive_gap_sec > self.jump_tolerance_sec
        )
        if receive_gapped:
            anomalies.append(
                {"kind": "receive_gap", "gapSec": round(receive_gap_sec, 4)}
            )

        # counter는 멀쩡한데 시간이 벌어졌다면 정확히 modulus 배수가 유실된 것임.
        # 이 조합이 없으면 15초 정지(1920샘플)가 clock jump와 구분되지 않음.
        if (receive_gapped or time_jumped) and not counter_gapped:
            anomalies.append(
                {
                    "kind": "full_cycle_loss_suspected",
                    "streamStopped": receive_gapped,
                }
            )

        try:
            if int(interpolated):
                self.interpolated_samples += 1
        except (TypeError, ValueError, OverflowError):
            # 조용히 삼키면 보간 집계가 왜 비었는지 사후에 알 수 없음
            anomalies.append(
                {"kind": "interpolated_invalid", "value": repr(interpolated)}
            )

        return anomalies

    def summary(self) -> dict:
        """종료 시점 누적 집계 반환함."""
        return {
            "eegEvents": self.events_seen,
            # 확정값 아님 — modulus 순환분은 세지 못함
            "lostSamplesLowerBound": self.lost_samples_lower_bound,
            "interpolatedSamples": self.interpolated_samples,
        }


def _is_finite_number(value: object) -> bool:
    """유한한 수치로 변환 가능한 값인지 판정함."""
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError, OverflowError):
        return False


def coerce_eeg_channels(
    eeg_row: list, indices: list[int]
) -> tuple[list[float] | None, str | None]:
    """EEG 행에서 대상 채널만 float로 변환하고 유효성 검사함.

    PR #38이 분석 단계(하류)에 넣은 유한성 방어를 CSV 생성층까지 이은 것임.
    비유한 값이 버퍼에 들어가면 128샘플 평균 전체가 오염돼 그 초의 5대역이
    통째로 무효가 되므로, 샘플 단위에서 걸러냄.

    Args:
        eeg_row - Cortex가 보낸 EEG 행임 (메타 채널 포함)
        indices - 추출할 뇌파 채널 인덱스 목록임

    Returns:
        성공 시 (값 리스트, None), 실패 시 (None, 사유) 반환.
    """
    if not indices:
        return None, "no_channel_mapping"
    if max(indices) >= len(eeg_row):
        return None, "row_too_short"

    values: list[float] = []
    for i in indices:
        try:
            value = float(eeg_row[i])
        except (TypeError, ValueError):
            return None, "non_numeric"
        if not np.isfinite(value):
            return None, "non_finite"
        values.append(value)
    return values, None


def build_met_map(labels: list[str]) -> dict[str, int]:
    """Cortex met 라벨 배열에서 지표별 인덱스 매핑 생성함.

    Args:
        labels - Cortex가 보낸 met 스트림 라벨 배열임

    Returns:
        지표명을 라벨 인덱스로 매핑한 dict 반환. 후보 라벨을 하나도
        찾지 못한 지표는 키 자체가 빠짐(호출부가 미발견을 감지 가능함).
    """
    met_map: dict[str, int] = {}
    for met_key, candidates in MET_LABEL_CANDIDATES.items():
        for label in candidates:
            if label in labels:
                met_map[met_key] = labels.index(label)
                break
    return met_map


class MindSignalStreamer(Cortex):
    """
    [Engine] EEG 시계열 버퍼링 및 분석을 통해 실시간 데이터를 발행하는 스트리머임
    """

    def __init__(
        self,
        group_id,
        subject_index,
        *args,
        headset_id="",
        alignment_location: str = "be",
        proxy_url: str | None = None,
        engine_secret_key: str = "",
        max_proxy_post_retries: int = 2,
        proxy_health_poll_interval_sec: float = 1.0,
        proxy_fail_closed_threshold_ms: int = 3000,
        **kwargs,
    ):
        if headset_id:
            kwargs["headset_id"] = headset_id
        super().__init__(*args, **kwargs)
        self.analyzer = MindSignalAnalyzer()

        # 1. 식별 정보 및 실험 설정 저장함
        self.group_id = group_id
        try:
            self.subject_index = int(subject_index)
        except ValueError:
            self.subject_index = 0

        self.duration_min = int(os.getenv("EXPERIMENT_DURATION_MINUTES", 10))
        self.duration_sec = self.duration_min * 60

        # proxy 연동 설정 저장함 (alignment_location="proxy" 시 proxy 모드 활성화)
        self.alignment_location = alignment_location
        self.proxy_url = proxy_url
        self.engine_secret_key = engine_secret_key
        self.max_proxy_post_retries = max_proxy_post_retries
        self.proxy_health_poll_interval_sec = proxy_health_poll_interval_sec
        self.proxy_fail_closed_threshold_ms = proxy_fail_closed_threshold_ms

        # 엔진 단위 단조 증가 시퀀스 카운터 초기화함 (proxy 모드 전용)
        self.seq = 0

        # fail-closed 중복 발동 방지 플래그 초기화함
        self._fail_closed_triggered = False

        # 2. Redis 채널 설정 및 연결 수행함 (REDIS_URL 우선, 없으면 HOST/PORT 폴백)
        self.channel = f"mind-signal:{self.group_id}:subject:{self.subject_index}"
        default_host = os.getenv("REDIS_HOST", "localhost")
        default_port = os.getenv("REDIS_PORT", 6379)
        fallback = f"redis://{default_host}:{default_port}/0"
        redis_url = os.getenv("REDIS_URL", fallback)
        self.r = redis.from_url(
            redis_url,
            retry=Retry(ExponentialBackoff(cap=10, base=1), 25),
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError],
            health_check_interval=1,
        )

        # 3. CSV 저장 경로 및 헤더 설정 수행함
        base_path = Path(__file__).resolve().parent.parent.parent
        csv_dir = os.path.join(base_path, "csv")

        if not os.path.exists(csv_dir):
            os.makedirs(csv_dir)
            print(f"데이터 저장 폴더 생성됨: {csv_dir}")

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"subject_{self.subject_index}_{self.group_id}_{timestamp_str}.csv"
        self.csv_path = os.path.join(csv_dir, filename)

        self.csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.csv_file)

        # 앞 12열은 기존 스키마 그대로 유지함. 하류(엔진 분석, markdown, 유사도,
        # BE coverage)가 이 이름들을 화이트리스트로 읽으므로 순서와 이름을
        # 바꾸지 않음. 5대역 값은 채널 공간 평균 기준임.
        # 뒤 25열은 채널별 대역 파워임. 부위별 분석을 위해 평균 전 값을 함께 남김.
        # 미지 컬럼은 하류 화이트리스트 파서가 무시하므로 추가로 인한 회귀가 없음.
        header = [
            "timestamp",
            *BAND_NAMES,
            "focus",
            "engagement",
            "interest",
            "excitement",
            "stress",
            "relaxation",
            *[f"{ch}_{band}" for ch in TARGET_EEG_CHANNELS for band in BAND_NAMES],
        ]
        self.writer.writerow(header)

        # 4. 런타임 매핑, 상태 변수 및 버퍼 초기화 수행함
        self.met_map = {}
        self.eeg_channel_indices = []
        # eeg_channel_indices와 같은 순서의 채널 이름임. 버퍼 열과 채널을 잇는 키임
        self.eeg_channel_names = []
        self.eeg_buffer = []

        # 스트림 연속성 계측 상태 초기화함 (P0-1). COUNTER와 INTERPOLATED 인덱스는
        # eeg 라벨 이벤트 수신 시 채워짐.
        self.counter_index: int | None = None
        self.interpolated_index: int | None = None
        self.continuity = StreamContinuityTracker()
        self.csv_rows_written = 0
        self.dropped_samples = 0
        self.dropped_blocks = 0
        self._continuity_log_counts: dict[str, int] = {}
        # 직전 롤업 시점의 이상 누적치임. 구간 증분 계산용
        self._rollup_prev_counts: dict[str, int] = {}
        self._rollup_active = False
        # 롤업 스레드와 종료 경로가 _log_rollup을 동시에 부르면 증분 계산의
        # 스냅샷과 교체 사이가 갈라져 anomalyDelta가 중복 계상되거나 구간
        # 경계가 깨짐. 두 호출을 직렬화함
        self._rollup_lock = threading.Lock()
        # sleep 대신 이 이벤트를 기다림. 종료 시 최대 30초를 기다리지 않고
        # 즉시 깨어남
        self._rollup_stop = threading.Event()
        self._health_published_at: dict[str, float] = {}
        self._last_event_monotonic: float | None = None
        self._eeg_stale = False
        self._met_stale = False
        self.latest_met = {
            "focus": 0,
            "engagement": 0,
            "interest": 0,
            "excitement": 0,
            "stress": 0,
            "relaxation": 0,
        }

        # 5. 측정 시작 시간 및 watchdog 상태 초기화함
        self.start_time = time.time()
        self.last_data_time = time.time()
        # MET 스트림은 EEG와 별개 수신 시각을 추적함 (한쪽만 죽는 경우 감지용)
        self.last_met_time = time.time()
        self._watchdog_active = False
        self._watchdog_interval = 30  # 무데이터 감지 임계값(초)

        # 6. 필수 이벤트 바인딩 수행함
        self.bind(create_session_done=self.on_create_session_done)
        self.bind(new_data_labels=self.on_new_data_labels)
        self.bind(new_eeg_data=self.on_eeg_data_done)
        self.bind(new_met_data=self.on_new_met_data)
        self.bind(inform_error=self.on_inform_error)
        self.bind(warn_cortex_stop_all_sub=self.on_headset_disconnected)

        # 7. SIGTERM 핸들러 등록함 (외부 종료 시 CSV 저장 보장)
        signal.signal(signal.SIGTERM, self._handle_sigterm)

        print(f"스트리밍 채널 활성화됨: {self.channel}")
        print(f"데이터 저장 경로: {self.csv_path}")

    def _handle_query_headset(self, result_dic):
        """헤드셋 ID 불일치 시 단일 연결 헤드셋으로 폴백 수행함 (D6).

        sdk/cortex.py 의 동명 메서드를 오버라이드함. 다음 세 가지 경우를 처리함:
        - 설정 ID와 연결 헤드셋 ID가 일치 → 부모 메서드 위임 (기존 동작 보존)
        - 불일치이고 연결 헤드셋이 정확히 1대 → 해당 헤드셋으로 폴백 후 부모 위임
        - 불일치이고 연결 헤드셋이 0대 또는 2대 이상 → 에러 출력 후 즉시 종료

        sdk/ 수정 금지 제약에 따라 오버라이드로 구현함.

        Args:
            result_dic: query headset 응답 리스트 (sdk 부모 메서드와 동일한 인수).
        """
        # headset_id 미지정 시 connected 헤드셋을 우선 선택함.
        # SDK 기본 로직(super)은 headset_list[0](목록 첫 번째)를 무조건 골라, 첫 번째가
        # discovered면 이미 connected된 헤드셋을 두고 connect 루프에 빠짐
        # (2026-07-02 라이브: 8E9 discovered가 먼저라 connected 5B 미선택 + subscribe 실패).
        if not self.headset_id:
            connected = [
                ele["id"] for ele in result_dic if ele.get("status") == "connected"
            ]
            # PC당 실사용 헤드셋 1대 전제(2-PC). connected가 정확히 1대일 때만
            # 우선 지정함. 2대 이상은 오선택 위험이라 자동선택 skip하고 SDK 기본
            # 선택에 위임함 (codex 5.5 조건). connected 없으면 기존 폴백 유지.
            if len(connected) == 1:
                self.headset_id = connected[0]
                print(
                    f"[INFO] connected 헤드셋 {self.headset_id!a} 우선 선택함 "
                    f"(subject {self.subject_index})"
                )
            elif len(connected) > 1:
                print(
                    f"[WARN] connected 헤드셋 {len(connected)}대 ({connected})라 "
                    f"자동 우선선택 skip, SDK 기본 선택 위임 (subject {self.subject_index})"
                )
            super()._handle_query_headset(result_dic)
            return

        headset_ids = [ele["id"] for ele in result_dic]

        # 설정 ID가 이미 목록에 있으면 기존 동작 그대로 진행함 (backward-compat)
        if self.headset_id in headset_ids:
            super()._handle_query_headset(result_dic)
            return

        # 설정 ID가 목록에 없음 — 폴백 분기 진입함
        if len(headset_ids) == 1:
            fallback_id = headset_ids[0]
            print(
                f"[WARN] 설정 헤드셋 ID {self.headset_id!a} 미발견. "
                f"연결된 헤드셋 '{fallback_id}' 으로 폴백함 "
                f"(subject {self.subject_index})"
            )
            self.headset_id = fallback_id
            super()._handle_query_headset(result_dic)
        elif len(headset_ids) == 0:
            print(
                f"[ERROR] 설정 헤드셋 ID {self.headset_id!a} 미발견이고 "
                f"연결된 헤드셋도 없어 측정 불가. 종료함 (subject {self.subject_index})"
            )
            self.close()
        else:
            print(
                f"[ERROR] 설정 헤드셋 ID {self.headset_id!a} 미발견이고 "
                f"연결된 헤드셋이 {len(headset_ids)}대({headset_ids})라 "
                f"자동 선택 불가. 종료함 (subject {self.subject_index})"
            )
            self.close()

    def on_new_data_labels(self, *args, **kwargs):
        """MET 점수 및 EEG 채널 인덱스를 동적으로 매핑함"""
        data = kwargs.get("data")
        stream_name = data["streamName"]
        labels = data["labels"]

        if stream_name == "met":
            # 라벨 이벤트가 재수신되면 매핑을 통째로 교체함. update()로 병합하면
            # 이전 이벤트의 키와 인덱스가 남아 누락 경고가 무력화되고
            # on_new_met_data가 옛 인덱스로 엉뚱한 값을 읽음 (CodeRabbit PR #36).
            self.met_map = build_met_map(labels)
            missing = [k for k in MET_LABEL_CANDIDATES if k not in self.met_map]
            print(f"MET 인덱스 매핑 완료됨: {self.met_map}")
            if missing:
                print(f"[WARN] MET 라벨 미발견: {missing} (labels={labels})")

        elif stream_name == "eeg":
            # 이름과 인덱스를 같은 순서로 함께 보존함. 채널이 하나라도 빠지면
            # 인덱스 목록만으로는 버퍼 열이 어느 채널인지 알 수 없어 채널별
            # 대역 파워를 잘못된 컬럼에 쓰게 됨
            self.eeg_channel_names = [ch for ch in TARGET_EEG_CHANNELS if ch in labels]
            self.eeg_channel_indices = [
                labels.index(ch) for ch in self.eeg_channel_names
            ]
            print(
                f"EEG 채널 인덱스 매핑 완료됨: "
                f"{dict(zip(self.eeg_channel_names, self.eeg_channel_indices))}"
            )
            missing_eeg = [ch for ch in TARGET_EEG_CHANNELS if ch not in labels]
            if missing_eeg:
                print(f"[WARN] EEG 채널 미발견: {missing_eeg} (해당 컬럼은 공란임)")

            # 연속성 계측용 메타 채널 인덱스 확보함. 없으면 계측만 비활성되고
            # 측정 자체는 그대로 진행함.
            self.counter_index = (
                labels.index("COUNTER") if "COUNTER" in labels else None
            )
            self.interpolated_index = (
                labels.index("INTERPOLATED") if "INTERPOLATED" in labels else None
            )
            if self.counter_index is None:
                # print가 아니라 logger를 씀. Windows 기본 콘솔 인코딩(CP949)에서
                # print는 UnicodeEncodeError로 콜백을 죽일 수 있고, 그러면
                # "계측만 끄고 측정은 계속"이라는 이 경로의 의도가 깨짐.
                logger.warning(
                    "EEG COUNTER 라벨 미발견으로 연속성 계측 비활성함 "
                    f"(labels={labels}, subject {self.subject_index})"
                )

    def on_create_session_done(self, *args, **kwargs):
        print(f"세션 연결 성공하였음. {self.duration_min}분 측정을 시작함.")

        timer = threading.Timer(self.duration_sec, self.auto_stop)
        timer.daemon = True
        timer.start()

        # watchdog 타이머 시작함 (무데이터 감지)
        self._watchdog_active = True
        self._start_watchdog()

        # 30초 롤업 시작함 (상한 없는 전 구간 추이 기록)
        self._start_rollup_thread()

        # proxy 모드 시 health 폴링 데몬 시작함
        if self.alignment_location == "proxy":
            self._start_proxy_health_monitor()

        self.sub_request(["eeg", "met"])

    def _start_proxy_health_monitor(self):
        """proxy /health 폴링으로 fail-closed 감지 시 측정 중단 수행하는 데몬 스레드 시작함"""
        tracker = ProxyHealthTracker(self.proxy_fail_closed_threshold_ms)

        def _check():
            while self._watchdog_active:
                healthy = check_health(self.proxy_url)
                tripped = tracker.record(healthy, time.monotonic())
                if tripped and not self._fail_closed_triggered:
                    self._fail_closed_triggered = True
                    print(
                        "[FAIL_CLOSED] proxy /health fail-closed 지속으로"
                        f" 측정 중단함 (subject {self.subject_index})"
                    )
                    try:
                        self.close_session()
                    except Exception as e:
                        logger.warning(f"close_session 실패 (무시): {e}")
                    finally:
                        self.close()
                    break
                time.sleep(self.proxy_health_poll_interval_sec)

        t = threading.Thread(target=_check, daemon=True)
        t.start()

    def _publish_health(
        self, status: str, elapsed: float = 0, min_interval_sec: float = 0
    ) -> None:
        """headset_status 경보를 Redis로 발행함 (watchdog과 불량 블록 공용).

        Args:
            status - DE 상태 문자열임. BE는 disconnected만 보존하고 나머지를
                stale로 정규화함 (`stream-health.service.ts`)
            elapsed - 무신호 경과 초임
            min_interval_sec - 같은 status 재발행 최소 간격임. 0이면 제한 없음
                (watchdog 기존 동작 보존). 매 샘플 발생 가능한 경보에만 사용함
        """
        if min_interval_sec:
            # 수신 간격과 같은 이유로 monotonic 기준임. wall clock이 뒤로 조정되면
            # time.time() 차이가 음수가 되어 그 폭만큼 경보가 통째로 억제됨.
            mono = time.monotonic()
            last = self._health_published_at.get(status)
            if last is not None and mono - last < min_interval_sec:
                return
            self._health_published_at[status] = mono

        try:
            self.r.publish(
                self.channel,
                json.dumps(
                    {
                        "type": "headset_status",
                        "status": status,
                        "subjectIndex": self.subject_index,
                        "groupId": self.group_id,
                        "silentSeconds": round(elapsed),
                    }
                ),
            )
        except Exception as exc:  # noqa: BLE001 — 경보 발행 실패가 측정을 깨지 않음
            # 조용히 삼키면 경보 자체가 사라진 걸 아무도 모름 (CodeRabbit PR #36)
            logger.warning(
                f"[WATCHDOG] 상태 발행 실패 ({status}): {exc}"
                f" (subject {self.subject_index})"
            )

    def _start_watchdog(self):
        """무데이터 감지 watchdog 스레드 시작함"""

        def _check():
            while self._watchdog_active:
                now = time.time()

                eeg_elapsed = now - self.last_data_time
                if eeg_elapsed > self._watchdog_interval:
                    logger.warning(
                        f"[WATCHDOG] {eeg_elapsed:.0f}초간 EEG 데이터 미수신"
                        f" (subject {self.subject_index})"
                    )
                    self._publish_health("no_data", eeg_elapsed)
                    # 정지 시작 1회만 구조화 로그로 남김 (복구는 on_eeg_data_done에서)
                    if not self._eeg_stale:
                        self._eeg_stale = True
                        self._log_continuity(
                            "eeg_stale_start", {"silentSec": round(eeg_elapsed, 1)}
                        )

                # MET는 EEG와 별개 스트림임. EEG만 살아 있고 MET가 죽으면
                # latest_met이 마지막 값을 무한 반복해 CSV에 얼어붙은 지표가
                # 기록되는데, EEG 기준 watchdog만으로는 감지되지 않음
                # (2026-07-10 교차검토에서 식별함).
                met_elapsed = now - self.last_met_time
                if met_elapsed > self._watchdog_interval:
                    logger.warning(
                        f"[WATCHDOG] {met_elapsed:.0f}초간 MET 지표 미수신"
                        f" (subject {self.subject_index})"
                    )
                    self._publish_health("metrics_stale", met_elapsed)
                    if not self._met_stale:
                        self._met_stale = True
                        self._log_continuity(
                            "met_stale_start", {"silentSec": round(met_elapsed, 1)}
                        )

                time.sleep(10)

        t = threading.Thread(target=_check, daemon=True)
        t.start()

    def on_headset_disconnected(self, *args, **kwargs):
        """헤드셋 분리 시 알림 발행함 (자동 재연결 안 함)"""
        print(f"[ALERT] 헤드셋 분리 감지됨 (subject {self.subject_index})")
        try:
            status_msg = json.dumps(
                {
                    "type": "headset_status",
                    "status": "disconnected",
                    "subjectIndex": self.subject_index,
                    "groupId": self.group_id,
                }
            )
            self.r.publish(self.channel, status_msg)
        except (ConnectionError, TimeoutError, ConnectionResetError) as e:
            logger.warning(f"헤드셋 분리 알림 publish 실패: {e}")

    def on_new_met_data(self, *args, **kwargs):
        """수신된 MET 배열에서 매핑된 점수 값만 추출함"""
        data = kwargs.get("data")["met"]
        # MET 전용 watchdog 타임스탬프 갱신함 (EEG와 독립)
        now = time.time()
        if self._met_stale:
            self._met_stale = False
            self._log_continuity(
                "met_recovered", {"silentSec": round(now - self.last_met_time, 1)}
            )
        self.last_met_time = now
        # 비유한 MET 값은 반영하지 않고 직전 정상값을 유지함. 그대로 넣으면
        # CSV와 Redis payload에 NaN이 실려 하류 분석이 통째로 깨짐.
        for key, index in self.met_map.items():
            # 길이 부족을 무음 스킵하면 그 지표가 직전값으로 얼어붙은 채 계속
            # 기록돼 라벨 매핑이 어긋났다는 사실을 사후에 알 수 없음
            if index >= len(data):
                self._log_continuity(
                    "met_index_out_of_range",
                    {"metric": key, "index": index, "length": len(data)},
                )
                continue
            if _is_finite_number(data[index]):
                self.latest_met[key] = data[index]
            else:
                self._log_continuity(
                    "met_value_rejected",
                    {"metric": key, "value": repr(data[index])},
                )

    def _finite_or_blank(
        self, channel_powers: dict, channel: str, band: str
    ) -> float | str:
        """채널별 대역 값이 유한하면 그대로, 아니면 공란 반환함.

        평균값 5대역은 행 단위 유한성 검사를 이미 거치지만(non_finite_power로
        행 전체를 버림) 채널별 값은 그 검사 대상이 아님. 채널 하나가 비유한이어도
        나머지 채널은 유효하므로 행을 버리지 않고 해당 셀만 비움. 비우지 않으면
        "nan" 문자열이 CSV에 박혀 하류 파서를 오염시킴.

        Args:
            channel_powers: 채널 이름을 키로 하는 대역 파워 dict임
            channel: 대상 채널 이름임
            band: 대상 대역 이름임

        Returns:
            유한한 값 또는 빈 문자열 반환
        """
        value = channel_powers.get(channel, {}).get(band)
        if value is None:
            return ""
        if not np.isfinite(value):
            self._log_continuity(
                "channel_power_non_finite",
                {"channel": channel, "band": band, "value": repr(value)},
            )
            return ""
        return value

    def _start_rollup_thread(self) -> None:
        """30초 주기 롤업 스레드 기동함.

        개별 이벤트 로그는 종류별 상한에 걸려 초반에 소진됨. 2026-07-31 회차는
        receive_gap 20건을 측정 4.1초에서 29.1초 사이에 다 써버려 이후 6분이
        무기록이었고, 그래서 subject 1의 전달 지연 누적을 사후에 볼 수 없었음.

        이 롤업은 상한을 적용하지 않고 전 구간을 덮음. 콜백이 아니라 별도
        스레드인 이유: 스트림이 완전히 멎으면 콜백이 안 오므로 콜백 기반
        롤업은 바로 그 순간부터 침묵함. 스레드는 계속 찍으며 csvRows가 멈춘
        것을 보여줌.
        """
        self._rollup_active = True
        self._rollup_stop.clear()

        def loop() -> None:
            # wait는 이벤트가 설정되면 True를 반환함. 즉 종료 신호가 오면
            # 남은 대기를 건너뛰고 즉시 빠져나감
            while not self._rollup_stop.wait(CONTINUITY_ROLLUP_INTERVAL_SEC):
                try:
                    with self._rollup_lock:
                        self._log_rollup()
                except Exception as e:  # 롤업 실패가 측정을 멈추면 안 됨
                    logger.warning(f"[CONTINUITY] 롤업 실패 (무시): {e}")

        self._rollup_thread = threading.Thread(
            target=loop, daemon=True, name="continuity-rollup"
        )
        self._rollup_thread.start()

    def _log_rollup(self) -> None:
        """직전 구간의 누적 지표를 상한 없이 1행으로 남김.

        clockSkewSec가 핵심 필드임. 로컬 현재 시각에서 마지막 Cortex 취득
        시각을 뺀 값이라, 이 값이 선형 증가하면 전달이 실시간보다 느려 지연이
        쌓이는 것이고(2026-07-31 증상), 계단식으로 튀면 스트림이 멎은 것임.
        """
        now = time.time()
        last_cortex = self.continuity.prev_time
        skew = None if last_cortex is None else round(now - last_cortex, 3)
        counts = dict(self._continuity_log_counts)
        delta_counts = {
            k: v - self._rollup_prev_counts.get(k, 0)
            for k, v in counts.items()
            if v - self._rollup_prev_counts.get(k, 0) > 0
        }
        self._rollup_prev_counts = counts

        record = {
            "kind": "stream_rollup",
            "groupId": self.group_id,
            "subjectIndex": self.subject_index,
            "elapsedSec": round(now - self.start_time, 1),
            "csvRows": self.csv_rows_written,
            "eegEvents": self.continuity.events_seen,
            # 로컬 시각과 Cortex 시각의 차이임. 지연 누적 판별의 핵심 지표
            "clockSkewSec": skew,
            "droppedSamples": self.dropped_samples,
            "droppedBlocks": self.dropped_blocks,
            # 이번 구간에 새로 발생한 이상만. 전체 누적은 종료 summary에 있음
            "anomalyDelta": delta_counts,
        }
        logger.warning("[CONTINUITY] %s", json.dumps(record))

    def _log_continuity(self, kind: str, payload: dict) -> None:
        """연속성 이벤트를 구조화 로그 1행으로 남김 (종류별 상한 적용).

        Args:
            kind - 이벤트 종류 키임 (counter_gap, timestamp_jump 등)
            payload - 종류별 부가 필드임
        """
        seen = self._continuity_log_counts.get(kind, 0) + 1
        self._continuity_log_counts[kind] = seen
        if seen > CONTINUITY_LOG_CAP_PER_KIND:
            return

        record = {
            "kind": kind,
            "groupId": self.group_id,
            "subjectIndex": self.subject_index,
            "elapsedSec": round(time.time() - self.start_time, 3),
            **payload,
        }
        if seen == CONTINUITY_LOG_CAP_PER_KIND:
            record["logCapReached"] = True
        logger.warning("[CONTINUITY] %s", json.dumps(record))

    def _discard_partial_buffer(self, reason: str) -> None:
        """모으던 부분 버퍼를 버림 (연속되지 않은 구간의 이어붙임 방지).

        Args:
            reason - 폐기 사유임 (수신 공백 또는 불량 샘플 사유)
        """
        if not self.eeg_buffer:
            return
        self._log_continuity(
            "partial_buffer_discarded",
            {"reason": reason, "samples": len(self.eeg_buffer)},
        )
        self.eeg_buffer = []

    def on_eeg_data_done(self, *args, **kwargs):
        """EEG 샘플을 버퍼링하고 1초(128샘플) 도달 시 대역 파워 계산 수행함"""
        data = kwargs.get("data")
        eeg_row = data["eeg"]
        cortex_time = data["time"]

        # watchdog 타임스탬프 갱신함
        now = time.time()
        silent_sec = now - self.last_data_time
        self.last_data_time = now

        # 로컬 수신 간격은 monotonic으로 잼. wall clock이 조정돼도 흔들리지 않아야
        # "이벤트가 안 온 것"과 "Cortex 시각만 튄 것"을 가를 수 있음.
        mono = time.monotonic()
        receive_gap = None
        if self._last_event_monotonic is not None:
            receive_gap = mono - self._last_event_monotonic
        self._last_event_monotonic = mono

        # stale 복구 엣지 기록함. watchdog은 정지 시작만 알리고 복구는 알리지
        # 않아서, 다음 공백의 지속 시간을 사후에 알 수 없었음.
        if self._eeg_stale:
            self._eeg_stale = False
            self._log_continuity("eeg_recovered", {"silentSec": round(silent_sec, 1)})

        # 연속성 관측함 (COUNTER 라벨이 매핑된 경우에만)
        if self.counter_index is not None and self.counter_index < len(eeg_row):
            interpolated = 0
            if self.interpolated_index is not None and self.interpolated_index < len(
                eeg_row
            ):
                interpolated = eeg_row[self.interpolated_index]
            for anomaly in self.continuity.observe(
                eeg_row[self.counter_index], cortex_time, interpolated, receive_gap
            ):
                self._log_continuity(anomaly.pop("kind"), anomaly)

        # 채널 인덱스가 아직 매핑되지 않은 경우 대기함
        if not self.eeg_channel_indices:
            return

        # 수신이 끊겼다가 재개되면 남은 부분 버퍼를 버림. 그대로 두면 15초 떨어진
        # 두 구간이 하나의 128샘플 블록으로 이어붙어 연속 신호가 아닌 것을
        # 연속으로 간주해 FFT를 돌리게 됨.
        if receive_gap is not None and receive_gap > TIMESTAMP_JUMP_TOLERANCE_SEC:
            self._discard_partial_buffer("receive_gap")

        # 메타데이터를 제외한 순수 뇌파 채널 데이터만 추출하여 버퍼에 추가함.
        # 불량 샘플은 버퍼에 넣지 않고 버림 — 하나만 섞여도 128샘플 평균이
        # 오염돼 그 초의 5대역이 통째로 무효가 됨.
        channel_data, reject_reason = coerce_eeg_channels(
            eeg_row, self.eeg_channel_indices
        )
        if reject_reason:
            self.dropped_samples += 1
            self._log_continuity("eeg_sample_dropped", {"reason": reject_reason})
            self._publish_health(
                "invalid_data", min_interval_sec=HEALTH_REPUBLISH_INTERVAL_SEC
            )
            # 불량 샘플 전후를 이어붙이지 않도록 부분 버퍼도 함께 버림
            self._discard_partial_buffer(reject_reason)
            return
        self.eeg_buffer.append(channel_data)

        # 버퍼에 1초 분량(128 샘플)의 데이터가 모였을 때 분석 실행함
        if len(self.eeg_buffer) >= self.analyzer.fs:
            # (128샘플 x 채널 수) 형태의 배열 생성함
            buffer_arr = np.array(self.eeg_buffer)

            # 공간(채널) 평균을 내어 1차원 시간 신호(길이 128)로 변환함
            mean_eeg_time_series = np.mean(buffer_arr, axis=1)

            # 시계열 데이터의 PSD를 구해 대역 파워 계산함
            powers = self.analyzer.get_all_powers(mean_eeg_time_series)

            # 평균으로 뭉개기 전 채널별 대역 파워도 함께 산출함. 부위별 분석
            # (예: 전두엽 알파 대 후두엽 알파)이 평균값만으로는 불가능해서임.
            # 채널 하나가 불량이어도 나머지는 유효하므로 채널별로 독립 계산함
            channel_powers = {
                name: self.analyzer.get_all_powers(buffer_arr[:, col])
                for col, name in enumerate(self.eeg_channel_names)
            }

            # 5대역이 같은 PSD를 공유하므로 비유한 입력은 전 대역에 전파됨
            # (실측 확인). 그 행은 쓰지 않고 버림 — coverage 계약이
            # "요청 대역 중 하나라도 비유한이면 그 초 전체 무효"라 어차피 못 씀.
            # 시각도 함께 검사함. 불량 cortex_time은 fromtimestamp()에서 예외를 내
            # 콜백 자체를 죽임 (이후 이벤트가 전부 유실됨). 유한성만으로는 부족해서
            # (1e300은 유한하지만 변환 불가) 실제 변환을 시도해 판정함.
            block_reject = None
            timestamp = None
            if not all(np.isfinite(v) for v in powers.values()):
                block_reject = "non_finite_power"
            else:
                try:
                    timestamp = datetime.fromtimestamp(cortex_time)
                except (TypeError, ValueError, OverflowError, OSError):
                    block_reject = "non_finite_time"

            if block_reject:
                self.dropped_blocks += 1
                self._log_continuity(
                    "eeg_block_dropped",
                    {
                        "reason": block_reject,
                        "bands": [k for k, v in powers.items() if not np.isfinite(v)],
                    },
                )
                self._publish_health(
                    "invalid_data", min_interval_sec=HEALTH_REPUBLISH_INTERVAL_SEC
                )
                self.eeg_buffer = []
                return

            # Cortex 타임스탬프를 문자열로 변환함 (위 검사에서 변환된 값 재사용)
            formatted_time = timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")

            # 1. CSV 기록 수행함.
            # 채널별 컬럼은 TARGET_EEG_CHANNELS 순서로 고정하고, 헤드셋에 없는
            # 채널은 공란으로 남김. 헤더가 회차마다 달라지면 하류 파서가 깨짐
            self.writer.writerow(
                [
                    formatted_time,
                    *[powers[band] for band in BAND_NAMES],
                    self.latest_met["focus"],
                    self.latest_met["engagement"],
                    self.latest_met["interest"],
                    self.latest_met["excitement"],
                    self.latest_met["stress"],
                    self.latest_met["relaxation"],
                    *[
                        self._finite_or_blank(channel_powers, ch, band)
                        for ch in TARGET_EEG_CHANNELS
                        for band in BAND_NAMES
                    ],
                ]
            )
            self.csv_file.flush()
            self.csv_rows_written += 1

            # 2. alignment_location에 따라 proxy 또는 Redis로 샘플 발행함
            if self.alignment_location == "proxy":
                # proxy 모드 — 시퀀스 증가 후 HTTP로 포워딩함
                self.seq += 1
                try:
                    post_sample(
                        proxy_url=self.proxy_url,
                        secret_key=self.engine_secret_key,
                        group_id=self.group_id,
                        subject_idx=self.subject_index,
                        seq=self.seq,
                        payload=powers,
                        metrics=dict(self.latest_met),
                        sync_meta={"de_clock_domain": "monotonic_ns"},
                        max_retries=self.max_proxy_post_retries,
                    )
                except ProxyForwardError:
                    # retry 소진 — FAIL_CLOSED: 측정 즉시 중단함 (중복 발동 방지)
                    if not self._fail_closed_triggered:
                        self._fail_closed_triggered = True
                        print(
                            "[FAIL_CLOSED] proxy 전송 retry 소진으로 측정 중단함"
                            f" (subject {self.subject_index})"
                        )
                        try:
                            self.close_session()
                        except Exception as e:
                            logger.warning(f"close_session 실패 (무시): {e}")
                        finally:
                            self.close()
                    return
            else:
                # be 모드 (기본) — Redis pub/sub 발행함 (byte-identical 유지)
                payload = {
                    "type": "brain_sync_all",
                    "groupId": self.group_id,
                    "subjectIndex": self.subject_index,
                    "waves": powers,
                    "metrics": self.latest_met,
                    "time": formatted_time,
                }
                try:
                    self.r.publish(self.channel, json.dumps(payload))
                except (ConnectionError, TimeoutError, ConnectionResetError) as e:
                    logger.warning(f"Redis publish 실패 (CSV 저장은 계속): {e}")

            # 분석 완료 후 버퍼 초기화함 (비오버랩 방식)
            self.eeg_buffer = []

    def _handle_sigterm(self, signum, frame):
        """SIGTERM 수신 시 graceful shutdown 수행함"""
        elapsed = time.time() - self.start_time
        print(f"\nSIGTERM 수신됨. 측정 시간: {elapsed:.1f}초. 정리 시작함.")
        try:
            self.close_session()
        except Exception as e:
            logger.warning(f"close_session 실패 (무시): {e}")
        finally:
            self.close()

    def auto_stop(self):
        elapsed = time.time() - self.start_time
        print(
            f"\n{self.duration_min}분이 경과하여 측정을 자동으로 종료함."
            f" (실제 측정 시간: {elapsed:.1f}초)"
        )
        try:
            self.close_session()
        except Exception as e:
            logger.warning(f"close_session 실패 (무시): {e}")
        finally:
            self.close()

    def on_inform_error(self, *args, **kwargs):
        error_data = kwargs.get("error_data")
        print(f"에러 발생함: {error_data}")
        try:
            self.close_session()
        except Exception as e:
            logger.warning(f"close_session 실패 (무시): {e}")
        finally:
            self.close()

    def on_close(self, *args, **kwargs):
        self._watchdog_active = False
        # 롤업 스레드를 먼저 멈춘 뒤 마지막 구간(직전 롤업 이후)을 남김.
        # 순서가 중요함 — 스레드를 안 멈추고 기록하면 두 호출이 증분 계산의
        # 스냅샷과 교체 사이에서 갈라짐. 반대로 기록을 생략하면 30초 미만의
        # 꼬리 구간이 통째로 사라짐
        if getattr(self, "_rollup_active", False):
            self._rollup_active = False
            self._rollup_stop.set()
            thread = getattr(self, "_rollup_thread", None)
            if thread is not None and thread.is_alive():
                # 대기 중이면 이벤트로 즉시 깨어나고, 기록 중이면 그것이 끝날
                # 때까지만 기다림. 종료 경로를 오래 붙잡지 않도록 상한을 둠
                thread.join(timeout=5)
            try:
                with self._rollup_lock:
                    self._log_rollup()
            except Exception as e:
                logger.warning(f"[CONTINUITY] 종료 롤업 실패 (무시): {e}")
        elapsed = time.time() - self.start_time if hasattr(self, "start_time") else 0

        # 총 EEG 이벤트 대비 CSV 행 수를 남김. 128 이벤트당 1행이 정상이라
        # 비율이 어긋나면 이벤트 유실인지 버퍼 미충족인지 사후 판별 가능함.
        if hasattr(self, "continuity"):
            summary = self.continuity.summary()
            summary.update(
                {
                    "kind": "session_summary",
                    "groupId": self.group_id,
                    "subjectIndex": self.subject_index,
                    "elapsedSec": round(elapsed, 1),
                    "csvRows": self.csv_rows_written,
                    "droppedSamples": self.dropped_samples,
                    "droppedBlocks": self.dropped_blocks,
                    "anomalyCounts": dict(self._continuity_log_counts),
                }
            )
            logger.warning("[CONTINUITY] %s", json.dumps(summary))

        if hasattr(self, "csv_file") and not self.csv_file.closed:
            self.csv_file.close()
        # 2-PC 집계 — CSV 닫힌 직후 operator BE로 사본 업로드함 (soft-fail).
        # 양쪽 DE가 업로드해도 operator 자기 사본은 멱등 overwrite라 무해함.
        if getattr(self, "csv_path", None):
            upload_csv_to_backend(
                settings.backend_url, self.csv_path, self.engine_secret_key
            )
        print(f"프로그램이 안전하게 종료되었음. (총 측정 시간: {elapsed:.1f}초)")
