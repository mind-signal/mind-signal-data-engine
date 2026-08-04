"""Playwright E2E 테스트용 mock data-engine 서버임.

실기기(EMOTIV 헤드셋) 없이 Playwright Scenario 1-4를 실행하기 위한
독립 FastAPI 서버. 실제 DE(`server/`)는 수정하지 않음.

실행 예시:
  python scripts/mock_data_engine.py --subject-index 1 --port 8001
  python scripts/mock_data_engine.py --subject-index 2 --port 8002
"""

import argparse
import asyncio
import math
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# ──────────────────────────────────────────────
# CLI 인수 파싱 (모듈 import 시점에 확정함)
# ──────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="Playwright E2E용 mock data-engine 서버")
_parser.add_argument(
    "--subject-index",
    type=int,
    default=1,
    help="이 인스턴스가 담당하는 피실험자 인덱스 (1-based, 기본값: 1)",
)
_parser.add_argument(
    "--port",
    type=int,
    default=8001,
    help="서버 포트 (기본값: 8001)",
)
_parser.add_argument(
    "--engine-secret",
    type=str,
    default="change-me-in-production",
    help="X-Engine-Secret 헤더 검증용 시크릿 키",
)
_parser.add_argument(
    "--backend-url",
    type=str,
    default="http://localhost:5000",
    help="BE /register-dual 대상 URL",
)
_parser.add_argument(
    "--group-id",
    type=str,
    default=None,
    help="DUAL_2PC groupId. 지정 시 startup hook에서 /register-dual 호출",
)
_parser.add_argument(
    "--register-retry-max",
    type=int,
    default=10,
    help="BE 미기동 상태 대비 retry 횟수 (1초 간격)",
)
_args, _ = _parser.parse_known_args()

# ──────────────────────────────────────────────
# in-memory 상태 (Redis 대체)
# ──────────────────────────────────────────────
_stream_active: bool = False  # 현재 스트리밍 중 여부
_current_group_id: str | None = None  # 활성 groupId
_subject_index: int = _args.subject_index  # 이 인스턴스 담당 subjectIndex
_secret_key: str = _args.engine_secret


# ──────────────────────────────────────────────
# FastAPI lifespan — startup 시 /register-dual 호출함
# ──────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """mock DE 부팅 시 BE /register-dual 호출함."""
    if _args.group_id is None:
        yield
        return

    public_url = f"http://localhost:{_args.port}"
    payload = {
        "groupId": _args.group_id,
        "subjectIndex": _args.subject_index,
        "engineUrl": public_url,
        "secretKey": _args.engine_secret,
    }
    for attempt in range(_args.register_retry_max):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.post(
                    f"{_args.backend_url}/api/engine/register-dual",
                    json=payload,
                )
                resp.raise_for_status()
                print(
                    f"[mock DE subject={_args.subject_index}] "
                    f"register-dual OK: {resp.json()}"
                )
                break
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            if attempt < _args.register_retry_max - 1:
                print(
                    f"[mock DE subject={_args.subject_index}] "
                    f"register-dual retry {attempt + 1}: {e}"
                )
                await asyncio.sleep(1)
            else:
                raise SystemExit(
                    f"[mock DE subject={_args.subject_index}] "
                    f"register-dual 실패 (max retry): {e}"
                )

    yield


# ──────────────────────────────────────────────
# FastAPI 앱 초기화
# ──────────────────────────────────────────────
app = FastAPI(
    title=f"Mind Signal Mock DE (subject {_subject_index})",
    version="mock-1.0.0",
    lifespan=_lifespan,
)


# ──────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────
def _check_secret(x_engine_secret: str) -> None:
    """X-Engine-Secret 헤더 검증함. 불일치 시 403 발생함."""
    if x_engine_secret != _secret_key:
        raise HTTPException(
            status_code=403, detail="인증 실패: 유효하지 않은 시크릿 키임"
        )


def _make_sin_wave(band: str, offset: float = 0.0) -> float:
    """현재 시각 기반 sin wave 합성 EEG 값 반환함.

    Args:
        band: 뇌파 대역명 (alpha/beta/theta/gamma 등)
        offset: 피실험자별 위상 오프셋 (rad)

    Returns:
        0.1~1.0 범위의 합성 파워 값
    """
    freqs = {"alpha": 0.5, "beta": 1.2, "theta": 0.3, "gamma": 2.0}
    freq = freqs.get(band, 0.7)
    t = time.time()
    raw = math.sin(2 * math.pi * freq * t + offset) * 0.4 + 0.5
    return round(max(0.1, min(1.0, raw)), 4)


def _make_metrics(offset: float = 0.0) -> dict[str, float]:
    """피실험자별 합성 Emotiv 지표 dict 반환함."""
    t = time.time()
    return {
        "focus": round(0.5 + 0.3 * math.sin(t * 0.4 + offset), 4),
        "engagement": round(0.6 + 0.2 * math.sin(t * 0.6 + offset + 1), 4),
        "interest": round(0.4 + 0.3 * math.cos(t * 0.5 + offset), 4),
        "excitement": round(0.3 + 0.2 * math.sin(t * 0.8 + offset + 2), 4),
        "stress": round(0.4 + 0.15 * math.cos(t * 0.3 + offset + 3), 4),
        "relaxation": round(0.5 + 0.2 * math.sin(t * 0.25 + offset + 4), 4),
    }


def _make_waves(offset: float = 0.0) -> dict[str, float]:
    """피실험자별 합성 뇌파 대역 파워 dict 반환함."""
    return {
        "delta": _make_sin_wave("delta", offset),
        "theta": _make_sin_wave("theta", offset),
        "alpha": _make_sin_wave("alpha", offset),
        "beta": _make_sin_wave("beta", offset),
        "gamma": _make_sin_wave("gamma", offset),
    }


def _phase_offset() -> float:
    """subjectIndex 기반 위상 오프셋 반환함. 두 인스턴스 간 파형 구분함."""
    return (_subject_index - 1) * math.pi / 3


# ──────────────────────────────────────────────
# 엔드포인트 — Health
# ──────────────────────────────────────────────
@app.get("/health")
async def health_check():
    """서버 상태 + subject 정보 반환함"""
    return {
        "status": "ok",
        "service": "mind-signal-mock-data-engine",
        "subject_index": _subject_index,
        "stream_active": _stream_active,
    }


# ──────────────────────────────────────────────
# 엔드포인트 — Stream
# ──────────────────────────────────────────────
class StreamStartRequest(BaseModel):
    """스트리밍 시작 요청 스키마임"""

    group_id: str
    subject_index: int


class StreamStopRequest(BaseModel):
    """스트리밍 종료 요청 스키마임"""

    group_id: str
    subject_index: int


@app.post("/api/stream/start")
async def stream_start(
    body: StreamStartRequest,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """EEG 스트리밍 시작 mock 응답 반환함.

    실제 DE와 달리 core.main을 spawn하지 않고 상태만 변경함.
    """
    global _stream_active, _current_group_id
    _check_secret(x_engine_secret)

    if body.subject_index != _subject_index:
        raise HTTPException(
            status_code=409,
            detail=(
                f"이 인스턴스는 subject {_subject_index} 담당임. "
                f"요청된 subject {body.subject_index}와 불일치함"
            ),
        )

    if _stream_active:
        raise HTTPException(
            status_code=409,
            detail=f"subject {_subject_index} 스트리밍 이미 실행 중임",
        )

    _stream_active = True
    _current_group_id = body.group_id

    return {
        "status": "started",
        "group_id": body.group_id,
        "subject_index": _subject_index,
        "channel": (f"mind-signal:{body.group_id}:subject:{_subject_index}"),
    }


@app.post("/api/stream/stop")
async def stream_stop(
    body: StreamStopRequest,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """EEG 스트리밍 종료 mock 응답 반환함."""
    global _stream_active, _current_group_id
    _check_secret(x_engine_secret)

    if not _stream_active:
        raise HTTPException(
            status_code=404,
            detail=f"subject {_subject_index} 활성 스트리밍 없음",
        )

    _stream_active = False
    _current_group_id = None

    return {
        "status": "stopped",
        "group_id": body.group_id,
        "subject_index": _subject_index,
    }


@app.get("/api/stream/status")
async def stream_status(
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """현재 스트리밍 상태 반환함."""
    _check_secret(x_engine_secret)
    return {
        "subject_index": _subject_index,
        "active": _stream_active,
        "group_id": _current_group_id,
    }


# ──────────────────────────────────────────────
# 엔드포인트 — Control (Playwright runtime groupId 주입용)
# ──────────────────────────────────────────────
class AssignGroupRequest(BaseModel):
    group_id: str


@app.post("/control/assign-group")
async def assign_group(req: AssignGroupRequest):
    """Playwright runtime groupId 주입 + /register-dual trigger 처리함."""
    global _current_group_id
    _current_group_id = req.group_id

    public_url = f"http://localhost:{_args.port}"
    payload = {
        "groupId": req.group_id,
        "subjectIndex": _args.subject_index,
        "engineUrl": public_url,
        "secretKey": _args.engine_secret,
    }
    async with httpx.AsyncClient(timeout=3) as client:
        resp = await client.post(
            f"{_args.backend_url}/api/engine/register-dual",
            json=payload,
        )
        resp.raise_for_status()
    return {"status": "registered", "groupId": req.group_id}


# ──────────────────────────────────────────────
# 엔드포인트 — Analyze
# ──────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    """단순 분석 요청 스키마임"""

    group_id: str
    subject_indices: list[int]
    include_markdown: bool = False


@app.post("/api/analyze")
async def analyze(
    body: AnalyzeRequest,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """세션 분석 mock 응답 반환함 (BTI용 analyze 계약 재현함)."""
    _check_secret(x_engine_secret)
    offset = _phase_offset()
    subjects = [
        {
            "subject_index": idx,
            "waves_mean": _make_waves(offset + idx * 0.5),
            "metrics_mean": _make_metrics(offset + idx * 0.5),
        }
        for idx in body.subject_indices
    ]
    result: dict = {
        "group_id": body.group_id,
        "subjects": subjects,
        "synchrony_score": round(0.65 + 0.1 * math.sin(_phase_offset()), 4),
    }
    if body.include_markdown:
        result["markdown"] = (
            f"# Mock Analysis — {body.group_id}\n\n"
            f"Subject indices: {body.subject_indices}\n"
        )
    return result


class PipelineParams(BaseModel):
    """파이프라인 파라미터 스키마임"""

    stimulus_duration_sec: int = 60
    window_size_sec: int = 10
    n_stimuli: int = 10
    baseline_duration_sec: int = 30
    band_cols: list[str] = ["alpha", "beta", "theta", "gamma"]


class PipelineRequest(BaseModel):
    """파이프라인 분석 요청 스키마임 (BE engine-proxy.service.ts 계약 재현함)"""

    group_id: str
    subject_indices: list[int]
    params: PipelineParams = PipelineParams()
    satisfaction_scores: dict[int, float] | None = None
    include_markdown: bool = False
    mode: Literal["DUAL", "BTI", "DUAL_2PC"] = "DUAL"
    algorithm: str = "default"


def _make_subject_feature(
    subject_index: int, band_cols: list[str], offset: float
) -> dict:
    """피실험자별 feature 결과 dict 반환함."""
    baseline = {b: _make_sin_wave(b, offset) for b in band_cols}
    features = {f"s1_w1_{b}": _make_sin_wave(b, offset + 0.1) for b in band_cols}
    return {
        "subject_index": subject_index,
        "baseline": baseline,
        "features": features,
        "n_features": len(features),
    }


@app.post("/api/analyze/pipeline")
async def analyze_pipeline(
    body: PipelineRequest,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """파이프라인 분석 mock 응답 반환함.

    DUAL / BTI / DUAL_2PC 모드별 응답 형식을
    실제 DE `analyze.py` 계약 그대로 재현함.
    """
    _check_secret(x_engine_secret)
    offset = _phase_offset()
    band_cols = body.params.band_cols

    # DUAL / BTI / DUAL_2PC — run_full_pipeline 계약 재현함
    subjects = [
        _make_subject_feature(idx, band_cols, offset + idx * 0.5)
        for idx in body.subject_indices
    ]
    pair_features = {
        f"pair_{b}": round(
            (_make_sin_wave(b, offset) + _make_sin_wave(b, offset + 0.5)) / 2,
            4,
        )
        for b in band_cols
    }
    pipeline_params = {
        "stimulus_duration_sec": body.params.stimulus_duration_sec,
        "window_size_sec": body.params.window_size_sec,
        "n_stimuli": body.params.n_stimuli,
        "baseline_duration_sec": body.params.baseline_duration_sec,
        "band_cols": band_cols,
        "n_windows_per_stimulus": (
            body.params.stimulus_duration_sec // body.params.window_size_sec
        ),
        "total_features_per_subject": (
            body.params.n_stimuli
            * (body.params.stimulus_duration_sec // body.params.window_size_sec)
            * len(band_cols)
        ),
    }

    similarity_features: dict | None = None
    if body.mode == "DUAL_2PC":
        # DUAL_2PC: 메타데이터 포함함 (실제 DE analyze.py 계약 준수)
        similarity_features = {"mode": "DUAL_2PC"}

    result: dict = {
        "group_id": body.group_id,
        "subjects": subjects,
        "pair_features": pair_features,
        "y_score": round(1.2 + 0.3 * math.sin(offset), 4),
        "synchrony_score": round(0.65 + 0.1 * math.cos(offset), 4),
        "pipeline_params": pipeline_params,
        "markdown": None,
        "similarity_features": similarity_features,
    }

    if body.include_markdown:
        result["markdown"] = (
            f"# Mock Pipeline — {body.group_id} ({body.mode})\n\n"
            f"Subjects: {body.subject_indices}\n"
        )

    return result


# ──────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print(
        f"[mock-de] subject_index={_subject_index} port={_args.port} "
        f"secret={'***' if _secret_key else '(none)'}"
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=_args.port,
        log_level="info",
    )
