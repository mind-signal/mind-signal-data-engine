"""markdown 서비스 features_to_markdown 함수 단위 테스트 수행함"""

from server.services.markdown import features_to_markdown


class TestFeaturesToMarkdown:
    """[TS-ANALYSIS-07] 마크다운 포함 분석 응답 반환."""

    def test_returns_string(self, sample_features):
        """반환 타입이 str임"""
        result = features_to_markdown(1, sample_features)
        assert isinstance(result, str)

    def test_subject_header_present(self, sample_features):
        """'## Subject 1' 헤더 포함함"""
        result = features_to_markdown(1, sample_features)
        assert "## Subject 1" in result

    def test_stimulus_header_present(self, sample_features):
        """'### Stimulus N' 헤더가 stimulus 수만큼 포함됨"""
        result = features_to_markdown(1, sample_features)
        assert "### Stimulus 1" in result
        assert "### Stimulus 2" in result

    def test_markdown_table_structure(self, sample_features):
        """파이프(|) 구분자 기반 테이블 행 존재함"""
        result = features_to_markdown(1, sample_features)
        lines = result.split("\n")
        pipe_lines = [line for line in lines if "|" in line]
        assert len(pipe_lines) > 0

    def test_band_names_in_header(self, sample_features):
        """band 이름이 테이블 헤더에 포함됨"""
        result = features_to_markdown(1, sample_features)
        for band in ["alpha", "beta", "theta", "gamma"]:
            assert band in result

    def test_window_row_prefix(self, sample_features):
        """데이터 행이 '| W' 형태로 시작함"""
        result = features_to_markdown(1, sample_features)
        lines = result.split("\n")
        w_lines = [line for line in lines if line.strip().startswith("| W")]
        assert len(w_lines) > 0

    def test_feature_count_footer(self, sample_features):
        """'총 feature 수: N개' 문구 포함함"""
        result = features_to_markdown(1, sample_features)
        assert f"총 feature 수: {len(sample_features)}개" in result

    def test_empty_features_no_crash(self):
        """빈 dict 전달 시 오류 없이 반환함"""
        result = features_to_markdown(1, {})
        assert isinstance(result, str)
        assert "## Subject 1" in result

    def test_malformed_key_ignored(self):
        """파싱 불가 키는 조용히 건너뜀"""
        features = {"bad_key": 0.5, "s1_w1_alpha": 0.1}
        result = features_to_markdown(1, features)
        assert "alpha" in result

    def test_multipart_band_name(self):
        """band가 언더스코어 포함 시 올바르게 파싱됨"""
        features = {"s1_w1_slow_alpha": 0.1234}
        result = features_to_markdown(1, features)
        assert "slow_alpha" in result
