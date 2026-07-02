"""POST /api/analyze/pipeline 엔드포인트 테스트"""

from unittest.mock import patch

import pytest  # noqa: F401

from tests.conftest import TEST_GROUP_ID, TEST_SECRET  # noqa: F401


class TestAnalyzePipelineEndpoint:
    """POST /api/analyze/pipeline 엔드포인트 검증함"""

    def test_missing_secret_header_returns_422(self, test_client):
        """Header 미제공 시 FastAPI validation error (422) 반환함"""
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            # X-Engine-Secret 헤더 미포함
        )
        assert response.status_code == 422

    def test_wrong_secret_returns_403(self, test_client):
        """잘못된 secret → 403 반환함"""
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers={"X-Engine-Secret": "wrong-secret"},
        )
        assert response.status_code == 403

    @patch("server.services.analysis.run_full_pipeline")
    def test_valid_request_returns_200(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """올바른 요청 → 200 반환함"""
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers=pipeline_secret_header,
        )
        assert response.status_code == 200

    @patch("server.services.analysis.run_full_pipeline")
    def test_response_group_id_matches(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """응답 JSON의 group_id가 요청과 일치함"""
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers=pipeline_secret_header,
        )
        data = response.json()
        assert data["group_id"] == TEST_GROUP_ID

    @patch("server.services.analysis.run_full_pipeline")
    def test_response_has_pipeline_params(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """응답 pipeline_params에 필수 키 존재함"""
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers=pipeline_secret_header,
        )
        data = response.json()
        assert "pipeline_params" in data
        assert "stimulus_duration_sec" in data["pipeline_params"]

    @patch("server.services.analysis.run_full_pipeline")
    def test_include_markdown_true(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """include_markdown=true 시 markdown 필드가 None이 아님"""
        # features_to_markdown이 호출되려면 subjects에 features가 있어야 함
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={
                "group_id": TEST_GROUP_ID,
                "subject_indices": [1, 2],
                "include_markdown": True,
            },
            headers=pipeline_secret_header,
        )
        data = response.json()
        assert data["markdown"] is not None

    @patch("server.services.analysis.run_full_pipeline")
    def test_include_markdown_false(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """include_markdown=false(기본값) 시 markdown=None임"""
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers=pipeline_secret_header,
        )
        data = response.json()
        assert data["markdown"] is None

    @patch("server.services.analysis.run_full_pipeline")
    def test_satisfaction_scores_returns_y_score(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """satisfaction_scores 포함 요청 시 y_score가 None이 아님"""
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={
                "group_id": TEST_GROUP_ID,
                "subject_indices": [1, 2],
                "satisfaction_scores": {"1": 7.5, "2": 6.0},
            },
            headers=pipeline_secret_header,
        )
        data = response.json()
        assert data["y_score"] is not None

    @patch("server.services.analysis.run_full_pipeline")
    def test_csv_not_found_returns_partial_200(
        self, mock_pipeline, test_client, pipeline_secret_header
    ):
        """한쪽 subject CSV 미발견 시 500 대신 200 + error 담은 partial 응답 반환함.

        2-PC에서 원격 subject가 미수집되면 run_full_pipeline이 그 subject를
        {subject_index, error}로 담음. SubjectFeatureResult가 feature 필드를 필수로
        요구하면 여기서 ValidationError로 500이 났음(회귀 방지).
        """
        mock_pipeline.return_value = {
            "group_id": TEST_GROUP_ID,
            "subjects": [
                {
                    "subject_index": 1,
                    "baseline": {"alpha": 0.5},
                    "features": {},
                    "n_features": 0,
                },
                {"subject_index": 2, "error": "CSV 파일 미발견"},
            ],
            "pair_features": None,
            "y_score": None,
            "synchrony_score": None,
            "pipeline_params": {
                "stimulus_duration_sec": 60,
                "window_size_sec": 10,
                "n_stimuli": 10,
                "baseline_duration_sec": 30,
                "band_cols": ["alpha"],
                "n_windows_per_stimulus": 6,
                "total_features_per_subject": 0,
            },
            "dataframes": {},
        }
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers=pipeline_secret_header,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["subjects"][1]["error"] == "CSV 파일 미발견"
        assert data["subjects"][1]["features"] is None

    def test_invalid_body_missing_group_id(self, test_client, pipeline_secret_header):
        """group_id 미포함 body → 422 validation error 반환함"""
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"subject_indices": [1, 2]},  # group_id 누락
            headers=pipeline_secret_header,
        )
        assert response.status_code == 422


class TestAnalyzePipelineModeField:
    """mode / algorithm 필드 검증 테스트 수행함"""

    @patch("server.services.analysis.run_full_pipeline")
    def test_omitting_mode_defaults_to_dual(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """mode 미포함 body → DUAL 기본값 적용, 기존 동작 유지함"""
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers=pipeline_secret_header,
        )
        assert response.status_code == 200

    @patch("server.services.analysis.analyze_pipeline_sequential")
    def test_mode_sequential_accepted(
        self,
        mock_seq_pipeline,
        test_client,
        pipeline_secret_header,
    ):
        """mode=SEQUENTIAL body → 422 없이 요청 수락함"""
        mock_seq_pipeline.return_value = {
            "group_id": TEST_GROUP_ID,
            "subjects": [],
            "similarity_features": {
                "algorithm": "cosine_pearson_faa",
                "similarity_score": 0.8,
                "overall_cosine": 0.6,
                "band_ratio_diff": {
                    "delta": 0.1,
                    "theta": 0.2,
                    "alpha": 0.05,
                    "beta": 0.15,
                    "gamma": 0.1,
                },
                "faa_absolute_diff": None,
            },
            "pair_features": None,
            "y_score": None,
            "synchrony_score": None,
        }
        response = test_client.post(
            "/api/analyze/pipeline",
            json={
                "group_id": TEST_GROUP_ID,
                "subject_indices": [1, 2],
                "mode": "SEQUENTIAL",
            },
            headers=pipeline_secret_header,
        )
        # SEQUENTIAL 분기에서 올바르게 처리되면 200 반환함
        assert response.status_code == 200
        data = response.json()
        assert data["similarity_features"] is not None

    @patch("server.services.analysis.analyze_pipeline_sequential")
    def test_sequential_subject_indices_passed_through(
        self,
        mock_seq_pipeline,
        test_client,
        pipeline_secret_header,
    ):
        """mode=SEQUENTIAL + subject_indices=[1,2] → 서비스 호출 시 subject_indices 전달됨"""
        mock_seq_pipeline.return_value = {
            "group_id": TEST_GROUP_ID,
            "subjects": [],
            "similarity_features": {
                "algorithm": "cosine_pearson_faa",
                "similarity_score": 0.7,
                "overall_cosine": 0.5,
                "band_ratio_diff": {
                    "delta": 0.1,
                    "theta": 0.1,
                    "alpha": 0.1,
                    "beta": 0.1,
                    "gamma": 0.1,
                },
                "faa_absolute_diff": None,
            },
            "pair_features": None,
            "y_score": None,
            "synchrony_score": None,
        }
        response = test_client.post(
            "/api/analyze/pipeline",
            json={
                "group_id": TEST_GROUP_ID,
                "subject_indices": [1, 2],
                "mode": "SEQUENTIAL",
            },
            headers=pipeline_secret_header,
        )
        assert response.status_code == 200
        # mock 호출 인수에 subject_indices=[1, 2]가 포함되어 있음
        _, call_kwargs = mock_seq_pipeline.call_args
        assert call_kwargs["subject_indices"] == [1, 2]

    def test_mode_invalid_returns_422(self, test_client, pipeline_secret_header):
        """mode=INVALID → 422 validation error 반환함"""
        response = test_client.post(
            "/api/analyze/pipeline",
            json={
                "group_id": TEST_GROUP_ID,
                "subject_indices": [1, 2],
                "mode": "INVALID",
            },
            headers=pipeline_secret_header,
        )
        assert response.status_code == 422

    @patch("server.services.analysis.run_full_pipeline")
    def test_response_allows_similarity_features_none(
        self, mock_pipeline, test_client, pipeline_secret_header, valid_pipeline_result
    ):
        """응답 similarity_features=None이어도 기존 DUAL 호출 정상 처리함"""
        mock_pipeline.return_value = valid_pipeline_result
        response = test_client.post(
            "/api/analyze/pipeline",
            json={"group_id": TEST_GROUP_ID, "subject_indices": [1, 2]},
            headers=pipeline_secret_header,
        )
        data = response.json()
        assert response.status_code == 200
        # similarity_features 필드가 없거나 None임
        assert data.get("similarity_features") is None
