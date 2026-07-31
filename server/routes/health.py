from fastapi import APIRouter, Request

from server.config import settings
from server.services.stream import get_all_status

router = APIRouter()


@router.get("/health")
async def health_check(request: Request):
    """서버 상태 확인 엔드포인트임 (대시보드 groupId 일치확인 + 측정중 노출)"""
    registered_group_id = getattr(request.app.state, "registered_group_id", None)
    # 실행 중인 스트리밍 프로세스 존재 여부 계산함 (측정중 판정)
    streaming = any(s.get("running") for s in get_all_status())
    return {
        "status": "ok",
        "service": "mind-signal-data-engine",
        "subject_index": settings.dual_2pc_subject_index,
        "registered_group_id": registered_group_id,
        "streaming": streaming,
    }
