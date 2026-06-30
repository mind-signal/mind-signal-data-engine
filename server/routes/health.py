from fastapi import APIRouter

from server.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    """서버 상태 확인 엔드포인트임 (대시보드용 subject_index 포함)"""
    return {
        "status": "ok",
        "service": "mind-signal-data-engine",
        "subject_index": settings.dual_2pc_subject_index,
    }
