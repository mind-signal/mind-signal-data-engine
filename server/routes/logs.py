"""로그 조회 라우터 — 대시보드에서 원격 DE 서버 로그를 가져오기 위한 엔드포인트임."""

from fastapi import APIRouter, Header, HTTPException, Query, Request

from server.services.logbuffer import tail

router = APIRouter()


@router.get("/logs")
async def get_logs(
    request: Request,
    n: int = Query(200, alias="tail", ge=1, le=1000),
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """최근 서버 로그 라인 반환함 (secret 검증).

    Args:
        n: 반환할 최근 라인 수 (기본 200, tail 쿼리 파라미터).

    Returns:
        lines 배열 + subject_index를 담은 dict.

    Raises:
        HTTPException 401 — secret 불일치 시.
    """
    if x_engine_secret != request.app.state.secret_key:
        raise HTTPException(status_code=401, detail={"error": "invalid_secret"})
    return {
        "subject_index": getattr(request.app.state, "subject_index", None),
        "registered_group_id": getattr(request.app.state, "registered_group_id", None),
        "lines": tail(n),
    }
