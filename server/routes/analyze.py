from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from server.config import settings
from server.services.analysis import compute_session_analysis
from server.services.markdown import dataframe_to_markdown

router = APIRouter()


class AnalyzeRequest(BaseModel):
    """분석 요청 페이로드 스키마임"""

    group_id: str
    subject_indices: list[int]  # [1, 2] 등
    include_markdown: bool = False  # LLM 전달용 MD 변환 포함 여부


class AnalyzeResponse(BaseModel):
    """분석 응답 페이로드 스키마임"""

    group_id: str
    subjects: list[dict]
    synchrony_score: float | None = None
    markdown: str | None = None


@router.post("/analyze")
async def analyze(
    body: AnalyzeRequest,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """백엔드로부터 분석 요청을 수신하여 결과를 반환함"""
    # 1. secret_key 검증 수행함
    if x_engine_secret != settings.engine_secret_key:
        raise HTTPException(
            status_code=403, detail="인증 실패: 유효하지 않은 시크릿 키임"
        )

    # 2. CSV 파일 탐색 및 분석 수행함
    result = compute_session_analysis(body.group_id, body.subject_indices)

    # 3. LLM용 Markdown 변환 (요청 시)
    if body.include_markdown:
        result["markdown"] = dataframe_to_markdown(result["dataframes"])

    # dataframes는 응답에서 제외함
    result.pop("dataframes", None)

    return AnalyzeResponse(**result)


class PipelineParams(BaseModel):
    """분석 파이프라인 파라미터임"""

    stimulus_duration_sec: int = Field(default=60, gt=0)
    window_size_sec: int = Field(default=10, gt=0)
    n_stimuli: int = Field(default=10, gt=0)
    baseline_duration_sec: int = Field(default=30, gt=0)
    band_cols: list[str] = Field(
        default_factory=lambda: ["alpha", "beta", "theta", "gamma"],
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_window_partition(self) -> "PipelineParams":
        """자극 길이가 윈도우 크기로 나누어떨어지는지 검증함"""
        if self.stimulus_duration_sec % self.window_size_sec != 0:
            raise ValueError("stimulus_duration_sec는 window_size_sec의 배수여야 함")
        return self


class PipelineRequest(BaseModel):
    """전체 파이프라인 분석 요청 페이로드 스키마임"""

    group_id: str
    subject_indices: list[int]
    params: PipelineParams = Field(default_factory=PipelineParams)
    satisfaction_scores: dict[int, float] | None = None  # {1: 7.5, 2: 6.0}
    include_markdown: bool = False
    mode: Literal["DUAL", "BTI", "DUAL_2PC"] = "DUAL"  # 분석 파이프라인 선택


class SubjectFeatureResult(BaseModel):
    """피실험자별 feature 추출 결과 스키마임

    한쪽 subject의 CSV가 없으면(2-PC에서 원격 subject 미수집 등) feature 필드 없이
    error만 채워 partial 응답으로 반환함. 이때 500 대신 200으로 내려 FE가 어느
    subject가 실패했는지 판단하게 함.
    """

    subject_index: int
    baseline: dict[str, float] | None = None
    features: dict[str, float] | None = None
    n_features: int | None = None
    error: str | None = None
    # 소비자가 자유 문장 대신 코드로 분기하도록 계약 위반 식별자를 함께 실음
    error_code: str | None = None


class PipelineResponse(BaseModel):
    """전체 파이프라인 분석 응답 페이로드 스키마임"""

    group_id: str
    subjects: list[SubjectFeatureResult]
    pair_features: dict[str, float] | None = None
    y_score: float | None = None
    synchrony_score: float | None = None
    # 정본 수식 점수임. 동조율에 100을 곱한 값이 아니라 민맥스 정규화 가중합이며
    # 상관 0이 50점임. score_params에 실제 사용값 원장이 실림
    friendship_score: float | None = None
    # 설정 원장(score_params)과 산출 메타(score_meta)를 분리함. 원장은 항상
    # 같은 키를 내고, 메타에는 사용한 열과 유효 쌍 수와 미산출 사유가 담김
    score_params: dict | None = None
    score_meta: dict | None = None
    pipeline_params: dict
    markdown: str | None = None
    # DUAL_2PC 경로가 메타데이터를 채움. 과거 SEQUENTIAL 문서에도 값이 있음
    similarity_features: dict | None = None


@router.post("/analyze/pipeline")
async def analyze_pipeline(
    body: PipelineRequest,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """알고리즘 명세 기반 전체 파이프라인 분석을 수행함"""
    # 1. secret_key 검증 수행함
    if x_engine_secret != settings.engine_secret_key:
        raise HTTPException(
            status_code=403, detail="인증 실패: 유효하지 않은 시크릿 키임"
        )

    # 2. 모드별 파이프라인 분기 실행함
    if body.mode == "DUAL_2PC":
        # v7 C-1: DUAL_2PC는 기존 run_full_pipeline 재활용 (DUAL/BTI와 동일 입력 구조)
        # BE가 두 subject 각각의 CSV를 정렬된 상태로 업로드하므로 subject_indices=[1,2] 전달
        from server.services.analysis import run_full_pipeline

        result = run_full_pipeline(
            group_id=body.group_id,
            subject_indices=body.subject_indices,  # BE가 [1, 2]로 전달
            stimulus_duration_sec=body.params.stimulus_duration_sec,
            window_size_sec=body.params.window_size_sec,
            n_stimuli=body.params.n_stimuli,
            baseline_duration_sec=body.params.baseline_duration_sec,
            band_cols=body.params.band_cols,
            satisfaction_scores=body.satisfaction_scores,
        )
        # mode 메타데이터 응답 포함 (FE 구분용)
        return PipelineResponse(
            group_id=body.group_id,
            subjects=result["subjects"],
            pair_features=result.get("pair_features"),
            y_score=result.get("y_score"),
            synchrony_score=result.get("synchrony_score"),
            friendship_score=result.get("friendship_score"),
            score_params=result.get("score_params"),
            score_meta=result.get("score_meta"),
            pipeline_params=result.get("pipeline_params", {}),
            similarity_features={"mode": "DUAL_2PC"},  # 메타데이터
        )

    # DUAL / BTI → 기존 파이프라인 실행함 (변경 없음)
    from server.services.analysis import run_full_pipeline

    result = run_full_pipeline(
        group_id=body.group_id,
        subject_indices=body.subject_indices,
        stimulus_duration_sec=body.params.stimulus_duration_sec,
        window_size_sec=body.params.window_size_sec,
        n_stimuli=body.params.n_stimuli,
        baseline_duration_sec=body.params.baseline_duration_sec,
        band_cols=body.params.band_cols,
        satisfaction_scores=body.satisfaction_scores,
    )

    # 3. LLM용 Markdown 변환 (요청 시)
    if body.include_markdown:
        from server.services.markdown import (
            dataframe_to_markdown,
            features_to_markdown,
        )

        md_sections = []
        # 기본 통계 Markdown
        if result.get("dataframes"):
            md_sections.append(dataframe_to_markdown(result["dataframes"]))
        # Feature 매트릭스 Markdown
        for subj in result.get("subjects", []):
            if "features" in subj:
                md_sections.append(
                    features_to_markdown(subj["subject_index"], subj["features"])
                )
        result["markdown"] = "\n\n---\n\n".join(md_sections)

    # dataframes는 응답에서 제외함
    result.pop("dataframes", None)

    return PipelineResponse(**result)
