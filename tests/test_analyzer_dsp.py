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
    """[회귀 A] 뇌파 0인 상수 입력은 전 대역 0이어야 함"""
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


def test_get_all_powers_routes_through_remove_dc(monkeypatch):
    """[회귀 D2] get_all_powers가 실제로 _remove_dc를 경유함.

    D1은 헬퍼 본문만 지키므로 헬퍼를 남겨둔 채 호출만 빼는 무력화를 놓침
    (그 상태에서 A/B/C/D1이 전부 통과함). 이 테스트가 그 경로를 지킴.
    """
    calls = []
    original = MindSignalAnalyzer._remove_dc

    def spy(eeg_values):
        calls.append(len(np.asarray(eeg_values)))
        return original(eeg_values)

    monkeypatch.setattr(MindSignalAnalyzer, "_remove_dc", staticmethod(spy))
    MindSignalAnalyzer().get_all_powers(_tone(10.0))
    assert calls == [128]  # 5대역에 재사용하므로 정확히 1회임


def test_band_sum_matches_total_rms():
    """파세발 관계 계약 — 경계 밖 단일 톤의 5대역 합이 참 RMS와 일치함.

    직사각형 합이 아니라 사다리꼴로 되돌리면 이 테스트가 실패함.
    """
    powers = MindSignalAnalyzer().get_all_powers(_tone(10.0))
    assert abs(sum(powers.values()) - TRUE_RMS) < 1e-06


def test_short_window_raises_instead_of_returning_zero():
    """창이 짧으면 조용한 0.0 대신 예외여야 함.

    빈 간격이 넓어지면 대역에 빈이 하나도 안 잡혀 톤이 있어도 0.0이 나옴.
    하류 유한성 검사가 그 0을 통과시키므로 진입점에서 막음.
    """
    with pytest.raises(ValueError):
        MindSignalAnalyzer().get_band_power(_tone(10.0, n=64), "alpha")


def test_synchrony_recovers_common_alpha_modulation():
    """[회귀 C] 공통 알파 변조를 가진 두 피실험자의 동조율이 0.90 이상이어야 함"""
    from server.services.analysis import compute_synchrony

    n_sec = 260
    modulation = 1.0 + 0.5 * np.sin(2 * np.pi * np.arange(n_sec) / 50.0)
    frames = [_synthesize_session(seed, n_sec, modulation) for seed in (11, 22)]
    score = compute_synchrony(frames[0], frames[1])
    assert score is not None
    assert score >= 0.90


def _synthesize_session(seed: int, n_sec: int, modulation: np.ndarray) -> pd.DataFrame:
    """공통 알파 변조와 개별 위상 및 잡음을 가진 합성 세션 DataFrame 생성함"""
    analyzer = MindSignalAnalyzer()
    rng = np.random.default_rng(seed)
    t = np.arange(128) / FS
    start = pd.Timestamp("2026-01-01 00:00:00")
    rows = []
    for i in range(n_sec):
        amplitude = TONE_AMPLITUDE * modulation[i] * (1.0 + 0.05 * rng.normal())
        window = (
            DC_LEVEL
            + 30.0 * rng.normal()
            + amplitude * np.sin(2 * np.pi * 10 * t + rng.uniform(0, 2 * np.pi))
            + rng.normal(0, 5.0, 128)
        )
        rows.append(
            {
                "timestamp": start + pd.Timedelta(seconds=i),
                **analyzer.get_all_powers(window),
            }
        )
    return pd.DataFrame(rows)
