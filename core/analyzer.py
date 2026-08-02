import numpy as np
from scipy.signal import welch
from scipy.stats import spearmanr


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

        # 2. Alpha 대역(8-13Hz) 추출.
        # 주의: BANDS의 alpha는 8-12Hz인데 여기는 8-13Hz임. FAA 문헌 관례를
        # 따른 것이라 의도적 불일치임. 현재 파이프라인에서 미호출 상태임
        alpha_mask = (freqs >= 8) & (freqs <= 13)
        alpha_power = np.mean(psd[:, alpha_mask], axis=1)

        # 3. 비대칭 지수 계산
        left_alpha = alpha_power[ch_left_idx]
        right_alpha = alpha_power[ch_right_idx]

        faa_score = np.log(right_alpha) - np.log(left_alpha)
        return faa_score

    @staticmethod
    def calculate_synchrony(
        user1_eeg: np.ndarray,
        user2_eeg: np.ndarray,
        method: str = "pearson",
    ) -> float:
        """두 사용자 간의 뇌파 동조율 계산함.

        **상관 계산은 이 메서드 한 곳으로만 들어감.** 호출부가 scipy를 직접
        부르면 여기를 mock한 기존 테스트가 조용히 무력화됨.

        Args:
            user1_eeg: 첫 피실험자 시계열임
            user2_eeg: 둘째 피실험자 시계열임
            method: "pearson" 또는 "spearman"임. 정본 수식은 스피어만 순위상관

        Returns:
            상관계수 반환. 한쪽이 상수면 nan이 나올 수 있으므로 호출부가
            유한성을 확인해야 함

        Raises:
            ValueError: 미지원 method임
        """
        if method not in ("pearson", "spearman"):
            raise ValueError(f"미지원 상관 방식 {method!r}")

        # 결측 제거를 두 경로에서 통일함. corrcoef는 NaN이 하나만 있어도
        # 전체가 nan이고 spearmanr(nan_policy="omit")은 쌍만 빼므로, 마스크를
        # 미리 걸지 않으면 같은 입력에서 방식마다 결과 의미가 달라짐
        left = np.asarray(user1_eeg, dtype=float)
        right = np.asarray(user2_eeg, dtype=float)
        mask = np.isfinite(left) & np.isfinite(right)
        left, right = left[mask], right[mask]
        if left.size < 2:
            return float("nan")

        if method == "pearson":
            return float(np.corrcoef(left, right)[0, 1])
        # 순위상관은 단조 변환에 불변이라 대역 값이 파워인지 RMS인지에
        # 영향받지 않음. scipy 1.15에서 결과 객체의 statistic을 씀
        return float(spearmanr(left, right).statistic)

    def __init__(self, sampling_rate: int = 128) -> None:
        # Emotiv Insight의 샘플링 레이트는 초당 128Hz임
        self.fs = sampling_rate

    @staticmethod
    def _remove_dc(eeg_values: np.ndarray) -> np.ndarray:
        """창별 평균을 빼서 DC 오프셋 제거함.

        Emotiv Insight의 부동 DC 준위는 약 4200uV이며 제조사 문서가 FFT류
        분석 전 DC 제거를 요구함. **이 단계가 파이프라인의 유일한 DC 제거자임**
        — welch는 detrend=False로 호출함. 제거자를 하나로 두어야 이 줄이
        실제로 부하를 지고 상수 입력 회귀 테스트가 그것을 지킴(둘을 겹쳐 두면
        어느 쪽을 없애도 결과가 같아 테스트가 무력해짐).

        Args:
            eeg_values: 1차원 EEG 시계열임

        Returns:
            평균이 0인 시계열 반환
        """
        data = np.asarray(eeg_values, dtype=float)
        return data - np.mean(data)

    def _band_rms(self, freqs: np.ndarray, psd: np.ndarray, band: str) -> float:
        """대역 PSD를 합산해 RMS 진폭으로 환산함.

        PSD 빈 합에 빈 간격을 곱한 값의 제곱근을 대역 RMS(uV)로 씀. 정확히는
        창(Hann) 제곱 가중 평균이라 단순 평균 제곱과 같지 않음 — 정상 신호에서
        근사 성립하고 대역 중앙 정현파에서는 정확히 일치하나, 추세가 강한
        신호에서는 어긋남(랜덤워크 실측 비율 0.33). 절대 교정값이 필요한
        용도에는 그대로 쓰지 말 것.

        사다리꼴(np.trapezoid)이 아니라 직사각형 합을 쓰는 이유: 사다리꼴은
        대역 경계 빈 가중치를 절반으로 깎아 delta 1Hz 회복률을 91.3%에서
        70.7%로 떨어뜨림. 회귀 테스트가 이 값으로 고정함.

        대역 경계는 닫힌 구간이라 경계 빈이 인접 두 대역에 모두 계산됨.
        광대역 신호에서 대역별 값의 합이 전체보다 커지지만, 두 피실험자에
        동일하게 걸리는 왜곡이라 코사인 유사도 영향은 1e-4 수준임.

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

        nperseg를 창 전체로 두므로 세그먼트가 1개임. 즉 평균화가 있는 Welch가
        아니라 **Hann 창 피리오도그램**이며 창당 추정 분산이 큼(백색잡음 실측
        변동계수 0.31). 세션 평균 지표는 수백 창 평균이라 무해하나 동조율은
        창 단위 시계열 상관이라 이 잡음이 상관을 0쪽으로 다소 감쇠시킴.
        겹침 평균(nperseg를 절반으로)은 이미 취약한 delta 해상도를 더 깎으므로
        택하지 않음.

        detrend=False인 이유: DC 제거는 _remove_dc 한 곳에서만 함.

        Raises:
            ValueError: 창이 MIN_WINDOW_SAMPLES 미만임
        """
        data = self._remove_dc(eeg_values)
        if data.size < self.MIN_WINDOW_SAMPLES:
            raise ValueError(
                f"창은 {self.MIN_WINDOW_SAMPLES}샘플 이상이어야 함. "
                f"받은 길이 {data.size}"
            )
        freqs, psd = welch(
            data,
            fs=self.fs,
            nperseg=len(data),
            detrend=False,
            scaling="density",
        )
        return {band: self._band_rms(freqs, psd, band) for band in self.BANDS}

    def get_all_powers(self, eeg_values: np.ndarray) -> dict[str, float]:
        """5개 대역의 RMS 강도를 한 번에 계산해 반환함.

        Args:
            eeg_values: 길이 MIN_WINDOW_SAMPLES 이상의 1차원 EEG 시계열임
                (채널 공간 평균)

        Returns:
            delta, theta, alpha, beta, gamma를 키로 하는 RMS(uV) dict 반환

        Raises:
            ValueError: 창이 MIN_WINDOW_SAMPLES 미만임
        """
        return self._band_powers_from(eeg_values)

    def get_band_power(self, eeg_values: np.ndarray, band: str) -> float:
        """단일 대역 RMS 반환함. 제거된 filter_* 계열의 대체 진입점임.

        Args:
            eeg_values: 길이 MIN_WINDOW_SAMPLES 이상의 1차원 EEG 시계열임
            band: BANDS의 키임

        Returns:
            해당 대역의 RMS 진폭(uV) 반환

        Raises:
            ValueError: 창이 너무 짧거나 미등록 대역임
        """
        if band not in self.BANDS:
            raise ValueError(f"미등록 대역 {band}")
        return self._band_powers_from(eeg_values)[band]
