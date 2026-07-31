import numpy as np
from scipy.signal import welch


class MindSignalAnalyzer:
    """EEG 대역 파워와 파생 지표 산출함.

    대역 파워는 대역통과 필터뱅크가 아니라 Welch PSD 대역 합산으로 구함.
    필터뱅크로 되돌리지 말 것 — 인과 필터는 128샘플 창에서 초기조건 0
    과도응답이 창 전체를 지배해 DC 크기에 비례하는 상수를 출력했고(alpha가
    항상 200 근처로 고정된 결함의 원인), 영위상 필터로 바꿔도 시작 위상에
    따라 회복률이 94.4에서 97.7%로 흔들림. 상세는 ANALYSIS-W001 참조.
    """

    BANDS: dict[str, tuple[float, float]] = {
        "delta": (0.5, 4.0),
        "theta": (4.0, 8.0),
        "alpha": (8.0, 12.0),
        "beta": (13.0, 30.0),
        "gamma": (30.0, 45.0),
    }

    # 이보다 짧은 창은 빈 간격이 넓어져 대역에 빈이 하나도 안 잡히고
    # 예외 없이 0.0이 반환됨. 하류 유한성 검사가 그 0을 통과시키므로 막음
    MIN_WINDOW_SAMPLES: int = 128

    @staticmethod
    def calculate_faa(eeg_data, ch_left_idx, ch_right_idx):
        """
        FAA(Frontal Alpha Asymmetry) 계산 로직
        공식: ln(Right Alpha) - ln(Left Alpha)
        """
        # 1. Welch 방법을 이용한 주파수 밀도(PSD) 계산
        fs = 128  # Emotiv Insight 샘플링 레이트
        freqs, psd = welch(eeg_data, fs, nperseg=fs * 2)

        # 2. Alpha 대역(8-13Hz) 추출
        alpha_mask = (freqs >= 8) & (freqs <= 13)
        alpha_power = np.mean(psd[:, alpha_mask], axis=1)

        # 3. 비대칭 지수 계산
        left_alpha = alpha_power[ch_left_idx]
        right_alpha = alpha_power[ch_right_idx]

        faa_score = np.log(right_alpha) - np.log(left_alpha)
        return faa_score

    @staticmethod
    def calculate_synchrony(user1_eeg, user2_eeg):
        """두 사용자 간의 뇌파 동기화(Correlation) 계산"""
        correlation = np.corrcoef(user1_eeg, user2_eeg)[0, 1]
        return correlation

    def __init__(self, sampling_rate: int = 128) -> None:
        # Emotiv Insight의 샘플링 레이트는 초당 128Hz임
        self.fs = sampling_rate

    @staticmethod
    def _remove_dc(eeg_values: np.ndarray) -> np.ndarray:
        """창별 평균을 빼서 DC 오프셋 제거함.

        Emotiv Insight의 부동 DC 준위는 약 4200uV이며 제조사 문서가 FFT류
        분석 전 DC 제거를 요구함. welch의 detrend="constant"가 같은 일을 다시
        하므로 운영 범위 입력에서 결과는 바뀌지 않으나, detrend 인자가 바뀌는
        경우의 안전망이자 극단 진폭에서의 부동소수 조건수 개선으로 유지함.

        Args:
            eeg_values: 1차원 EEG 시계열임

        Returns:
            평균이 0인 시계열 반환
        """
        data = np.asarray(eeg_values, dtype=float)
        return data - np.mean(data)

    def _band_rms(self, freqs: np.ndarray, psd: np.ndarray, band: str) -> float:
        """대역 PSD를 합산해 RMS 진폭으로 환산함.

        파세발 관계로 PSD 빈 합에 빈 간격을 곱한 값이 그 대역 성분의 평균
        제곱이므로 제곱근이 RMS임. 단위는 기존과 같은 uV임.

        사다리꼴(np.trapezoid)이 아니라 직사각형 합을 쓰는 이유: 이산 PSD
        빈에서 파세발 관계를 정확히 지키는 것은 직사각형 합임. 사다리꼴은
        대역 경계 빈 가중치를 절반으로 깎아 delta 1Hz 회복률을 91.3%에서
        70.7%로 떨어뜨림.

        대역 경계는 닫힌 구간임. 경계 주파수 성분이 인접 두 대역에 모두
        계산되지만, 반열린 구간으로 바꾸면 중앙 주파수 회복률이 떨어짐.

        Args:
            freqs: welch가 반환한 주파수 축임
            psd: welch가 반환한 전력 스펙트럼 밀도임
            band: BANDS의 키임

        Returns:
            해당 대역의 RMS 진폭(uV) 반환
        """
        low, high = self.BANDS[band]
        mask = (freqs >= low) & (freqs <= high)
        if not mask.any():
            return 0.0
        bin_width = float(freqs[1] - freqs[0])
        return float(np.sqrt(np.sum(psd[mask]) * bin_width))

    def _band_powers_from(self, eeg_values: np.ndarray) -> dict[str, float]:
        """단일 welch 호출로 5대역 RMS 산출함.

        nperseg를 창 전체로 두므로 세그먼트가 1개이며 detrend="constant"가
        그 세그먼트의 평균을 뺌.
        """
        data = self._remove_dc(eeg_values)
        freqs, psd = welch(
            data,
            fs=self.fs,
            nperseg=len(data),
            detrend="constant",
            scaling="density",
        )
        return {band: self._band_rms(freqs, psd, band) for band in self.BANDS}

    def get_rms_power(self, filtered_data: np.ndarray) -> float:
        """이미 필터링된 신호의 강도(RMS) 반환함"""
        return float(np.sqrt(np.mean(np.square(filtered_data))))

    def get_all_powers(self, eeg_values: np.ndarray) -> dict[str, float]:
        """5개 대역의 RMS 강도를 한 번에 계산해 반환함.

        Args:
            eeg_values: 길이 128의 1차원 EEG 시계열(채널 공간 평균)임

        Returns:
            delta, theta, alpha, beta, gamma를 키로 하는 RMS(uV) dict 반환
        """
        return self._band_powers_from(eeg_values)

    def get_band_power(self, eeg_values: np.ndarray, band: str) -> float:
        """단일 대역 RMS 반환함. 제거된 filter_* 계열의 대체 진입점임.

        Args:
            eeg_values: 1차원 EEG 시계열임. 길이 MIN_WINDOW_SAMPLES 이상이어야 함
            band: BANDS의 키임

        Returns:
            해당 대역의 RMS 진폭(uV) 반환

        Raises:
            ValueError: 창이 너무 짧거나 미등록 대역임
        """
        values = np.asarray(eeg_values, dtype=float)
        if values.size < self.MIN_WINDOW_SAMPLES:
            raise ValueError(
                f"창은 {self.MIN_WINDOW_SAMPLES}샘플 이상이어야 함. "
                f"받은 길이 {values.size}"
            )
        if band not in self.BANDS:
            raise ValueError(f"미등록 대역 {band}")
        return self._band_powers_from(values)[band]
