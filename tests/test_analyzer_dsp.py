"""MindSignalAnalyzer DSP 계약 회귀 테스트

전 테스트가 합성 신호 기반임. 기존 CSV는 결함 산출물이라 기준선으로 못 씀
(원시 EEG 미보존으로 재계산 불가 — ANALYSIS-W001 3.1절).
"""

import numpy as np
import pandas as pd
import pytest

from core.analyzer import MindSignalAnalyzer

FS = 128
DC_LEVEL = 4200.0  # Emotiv Insight 부동 DC 준위 약 4200uV
TONE_AMPLITUDE = 20.0  # 합성 정현파 진폭(uV)
TRUE_RMS = TONE_AMPLITUDE / np.sqrt(2)  # 14.1421


def _tone(freq: float, dc: float = DC_LEVEL, n: int = 128, phase: float = 0.0):
    """DC 오프셋 위에 단일 정현파를 얹은 1초 창 생성함"""
    t = np.arange(n) / FS
    return dc + TONE_AMPLITUDE * np.sin(2 * np.pi * freq * t + phase)


def test_constant_dc_input_yields_zero_band_power():
    """[회귀 A] 뇌파 0인 상수 입력은 전 대역 0이어야 함.

    welch를 detrend=False로 호출하므로 _remove_dc가 유일한 DC 제거자임.
    그 호출을 빼면 이 테스트가 delta 약 2425로 실패함.
    """
    powers = MindSignalAnalyzer().get_all_powers(np.full(128, DC_LEVEL))
    assert max(powers.values()) < 1e-06


def test_alpha_rms_recovers_true_amplitude():
    """[회귀 B] 10Hz 20uV 알파의 RMS 회복률이 참값의 95~105%여야 함"""
    powers = MindSignalAnalyzer().get_all_powers(_tone(10.0))
    ratio = powers["alpha"] / TRUE_RMS
    assert 0.95 <= ratio <= 1.05


@pytest.mark.parametrize(
    "band,freq,min_selectivity",
    [
        ("delta", 2.0, 3.0),
        ("theta", 6.0, 3.0),
        ("alpha", 10.0, 3.0),
        ("beta", 20.0, 3.0),
        ("gamma", 38.0, 3.0),
    ],
)
def test_band_selectivity(band, freq, min_selectivity):
    """해당 대역 중앙 정현파를 넣으면 그 대역이 최대이고 차순위의 3배 이상이어야 함.

    대역 중앙만 보장함. 대역 경계(4Hz, 8Hz, 30Hz)는 닫힌 구간 설계라 인접
    대역과 동률이며 이는 알려진 성질임 — ANALYSIS-W001 결정 2 참조.
    """
    powers = MindSignalAnalyzer().get_all_powers(_tone(freq))
    runner_up = max(v for k, v in powers.items() if k != band)
    assert powers[band] == max(powers.values())
    # 채택안은 타 대역이 수치 잡음 수준이라 선택도가 매우 큼. 기준 3.0은
    # 설계가 필터뱅크로 되돌아가는 경우까지 커버하는 하한임
    assert runner_up == 0.0 or powers[band] / runner_up >= min_selectivity


@pytest.mark.parametrize("phase_index", range(8))
def test_alpha_recovery_is_phase_stable(phase_index):
    """[회귀 E] 시작 위상이 달라져도 알파 회복률이 95~105%를 유지해야 함.

    영위상 필터뱅크는 이 테스트를 통과하지 못함(위상에 따라 94.43%까지
    내려감). Welch 채택의 핵심 근거이므로 계약으로 고정함.
    """
    phase = 2 * np.pi * phase_index / 8
    window = _tone(10.0, phase=phase)
    ratio = MindSignalAnalyzer().get_all_powers(window)["alpha"] / TRUE_RMS
    assert 0.95 <= ratio <= 1.05


def test_dc_level_does_not_change_result():
    """DC 준위가 3000/4200/5000으로 달라져도 알파 값이 동일해야 함"""
    analyzer = MindSignalAnalyzer()
    values = [
        analyzer.get_all_powers(_tone(10.0, dc=dc))["alpha"]
        for dc in (3000.0, 4200.0, 5000.0)
    ]
    assert max(values) - min(values) < 1e-09


def test_remove_dc_returns_zero_mean():
    """[회귀 D1] DC 제거 헬퍼의 단위 계약 — 출력 평균이 0임"""
    out = MindSignalAnalyzer._remove_dc(_tone(10.0))
    assert abs(float(np.mean(out))) < 1e-09


def test_delta_recovery_pins_rectangular_quadrature():
    """[회귀 F] 대역 적분이 직사각형 합임을 delta 1Hz 회복률로 고정함.

    사다리꼴(np.trapezoid)은 대역 경계 빈 가중치를 절반으로 깎음. 1Hz 톤은
    에너지가 대역 하한 근처에 있어 둘을 판별함 — 직사각형 91.3%, 사다리꼴
    70.7%. 반면 대역 중앙 톤(예: alpha 10Hz)은 경계 빈이 0이라 두 방식의
    결과가 같으므로 판별에 쓸 수 없음.
    """
    delta = MindSignalAnalyzer().get_all_powers(_tone(1.0))["delta"]
    ratio = delta / TRUE_RMS
    assert 0.90 <= ratio <= 0.93


def test_bin_width_scaling_holds_for_non_unit_resolution():
    """[회귀 G] PSD 합에 빈 간격을 곱하는 스케일 인자를 고정함.

    1초 창(128샘플 at 128Hz)은 빈 간격이 정확히 1.0이라 곱을 빼도 결과가
    같음. 2초 창은 0.5가 되므로 스케일 인자 누락이 드러남(참값의 141%).
    """
    n = 256  # 2초 창이라 빈 간격 0.5Hz임
    t = np.arange(n) / FS
    window = DC_LEVEL + TONE_AMPLITUDE * np.sin(2 * np.pi * 10.0 * t)
    ratio = MindSignalAnalyzer().get_all_powers(window)["alpha"] / TRUE_RMS
    assert 0.95 <= ratio <= 1.05


def test_band_edges_are_closed_intervals():
    """대역 경계가 닫힌 구간임을 고정함.

    8Hz는 theta 상한이자 alpha 하한이라 두 대역에 같은 값으로 계산됨.
    반열린 구간으로 바꾸면 한쪽만 잡혀 이 계약이 깨짐. 중앙 주파수 정확도를
    위한 의도된 선택임 — ANALYSIS-W001 결정 2 참조.
    """
    powers = MindSignalAnalyzer().get_all_powers(_tone(8.0))
    assert powers["theta"] == pytest.approx(powers["alpha"], rel=1e-09)
    assert powers["alpha"] / TRUE_RMS > 0.5


@pytest.mark.parametrize("entry", ["get_all_powers", "get_band_power"])
def test_short_window_raises_instead_of_returning_zero(entry):
    """창이 짧으면 조용한 0.0 대신 예외여야 함.

    빈 간격이 넓어지면 대역에 빈이 안 잡혀 톤이 있어도 0.0이 나오거나
    (길이 16에서는) theta와 alpha가 나란히 11.86인 무의미한 값이 나옴.
    하류 유한성 검사가 그 값을 통과시키므로 진입점에서 막음. 실사용 진입점은
    get_all_powers 쪽이므로 두 경로 모두 검사함.
    """
    analyzer = MindSignalAnalyzer()
    short = _tone(10.0, n=64)
    with pytest.raises(ValueError):
        if entry == "get_all_powers":
            analyzer.get_all_powers(short)
        else:
            analyzer.get_band_power(short, "alpha")


def test_synchrony_recovers_common_alpha_modulation():
    """[회귀 C] 공통 알파 변조를 가진 두 피실험자의 동조율이 0.90 이상이어야 함

    이 픽스처는 공통 변조를 알파 톤에만 걸므로 대역을 alpha로 명시 주입함.
    정본 지표(감마)로 이 픽스처를 돌리면 감마는 잡음에서만 나와 상관이 0
    근처다 — 아래 감마 버전이 그 대비다.
    """
    from server.services.analysis import compute_synchrony
    from server.services.score_params import ScoreParams

    n_sec = 260
    modulation = 1.0 + 0.5 * np.sin(2 * np.pi * np.arange(n_sec) / 50.0)
    frames = [_synthesize_session(seed, n_sec, modulation) for seed in (11, 22)]
    score, meta = compute_synchrony(
        frames[0], frames[1], ScoreParams(sync_channels=(), sync_band="alpha")
    )
    assert score is not None
    assert score >= 0.90
    assert meta["sync_columns_used"] == ["alpha"]


def test_synchrony_recovers_common_gamma_modulation():
    """정본 지표(감마)로도 공통 변조가 있으면 동조율이 회복돼야 함

    광대역 잡음 진폭에 공통 변조를 걸어 감마 대역이 함께 움직이게 함.
    스피어만 순위상관은 단조 변환에 불변이라 대역 값이 RMS인지 파워인지에
    영향받지 않음.
    """
    from server.services.analysis import compute_synchrony
    from server.services.score_params import ScoreParams

    n_sec = 260
    # 알파 버전보다 변조를 깊게 검. 감마는 잡음에서만 나오고 단일 세그먼트
    # 피리오도그램이라 창당 추정 분산이 커(백색잡음 실측 변동계수 0.31)
    # 같은 변조 깊이에서는 상관이 알파보다 감쇠함
    modulation = 1.0 + 1.0 * np.sin(2 * np.pi * np.arange(n_sec) / 50.0)
    frames = [
        _synthesize_session(seed, n_sec, modulation, noise_modulated=True)
        for seed in (33, 44)
    ]
    score, meta = compute_synchrony(
        frames[0], frames[1], ScoreParams(sync_channels=(), sync_band="gamma")
    )
    assert score is not None
    assert score >= 0.90
    assert meta["sync_columns_used"] == ["gamma"]


def _synthesize_session(
    seed: int,
    n_sec: int,
    modulation: np.ndarray,
    noise_modulated: bool = False,
) -> pd.DataFrame:
    """공통 변조와 개별 위상 및 잡음을 가진 합성 세션 DataFrame 생성함

    noise_modulated면 광대역 잡음 진폭에도 같은 변조를 걸어 감마 대역이
    공통으로 움직이게 함.
    """
    analyzer = MindSignalAnalyzer()
    rng = np.random.default_rng(seed)
    t = np.arange(128) / FS
    start = pd.Timestamp("2026-01-01 00:00:00")
    rows = []
    for i in range(n_sec):
        amplitude = TONE_AMPLITUDE * modulation[i] * (1.0 + 0.05 * rng.normal())
        noise_scale = 5.0 * (modulation[i] if noise_modulated else 1.0)
        window = (
            DC_LEVEL
            + 30.0 * rng.normal()
            + amplitude * np.sin(2 * np.pi * 10 * t + rng.uniform(0, 2 * np.pi))
            + rng.normal(0, noise_scale, 128)
        )
        rows.append(
            {
                "timestamp": start + pd.Timedelta(seconds=i),
                **analyzer.get_all_powers(window),
            }
        )
    return pd.DataFrame(rows)
