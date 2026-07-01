"""Control 라우터 — DUAL_2PC runtime groupId 주입 + idle-rebind 자동복구 제공함 (Phase 17.5 + 19)"""

import asyncio

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from server.services.stream import get_all_status, stop_stream
from server.services.webhook import (
    register_to_backend_dual,
    start_heartbeat_dual,
)

router = APIRouter()


class AssignGroupRequest(BaseModel):
    """/control/assign-group 요청 바디 스키마임"""

    group_id: str = Field(
        ..., min_length=1, description="DUAL_2PC groupId (MongoDB ObjectId 24자 hex)"
    )


class ReleaseRequest(BaseModel):
    """/control/release 요청 바디 스키마임 (비상 해제 / BE supersede)"""

    group_id: str | None = Field(
        None,
        description="해제 대상 groupId. 미지정 시 현재 바인딩 무조건 해제함 (비상 전체해제)",
    )


def _is_streaming_for(group_id: str) -> bool:
    """해당 group_id로 실행 중인 스트리밍 프로세스 존재 여부 반환함 (측정중 판정)."""
    for s in get_all_status():
        key = s.get("key", "")
        if s.get("running") and key.startswith(f"{group_id}:"):
            return True
    return False


def _stop_streams_for(group_id: str) -> list[str]:
    """해당 group_id의 실행 중 스트림 전부 정지함 — orphan core.main 방지. 정지된 key 반환."""
    stopped: list[str] = []
    for s in get_all_status():
        key = s.get("key", "")
        if not (s.get("running") and key.startswith(f"{group_id}:")):
            continue
        gid, _, sidx = key.partition(":")
        try:
            stop_stream(gid, int(sidx))
            stopped.append(key)
        except (KeyError, ValueError):
            # 이미 정리됐거나 key 파싱 실패 — 무시함
            pass
    return stopped


def _release_binding(app) -> None:
    """현재 그룹 바인딩 해제함 — registered_group_id 초기화 + heartbeat 취소함."""
    task = getattr(app.state, "heartbeat_task", None)
    if task is not None:
        task.cancel()
        app.state.heartbeat_task = None
    app.state.registered_group_id = None


@router.post("/control/assign-group")
async def assign_group(
    body: AssignGroupRequest,
    request: Request,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """DUAL_2PC groupId 런타임 주입 + BE /register-dual 트리거 처리함.

    멱등 + idle-rebind 계약 (Phase 19 자동복구):
    - pending(null) → register-dual 후 200 registered + heartbeat 시작함
    - same group_id 재호출 → 200 already_registered (register/heartbeat 재생성 금지)
    - different group_id + **측정중 아님** → 옛 바인딩 supersede(해제) 후 새 그룹 등록 → 200 registered
    - different group_id + **측정중** → 409 group_id_conflict (라이브 측정 보호, DE가 최종 거부권)
    - secret mismatch → 401 invalid_secret
    - BE 실패 → 502 backend_register_failed + state pending 유지(재시도 가능)

    asyncio.Lock으로 race 방지함 — 동시 요청 직렬화 처리함.
    """
    app = request.app

    # secret 검증 (401)
    if x_engine_secret != app.state.secret_key:
        raise HTTPException(status_code=401, detail={"error": "invalid_secret"})

    # subject_index 설정 여부 확인 (pending 모드가 아니면 config 에러)
    if app.state.subject_index is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_pending_mode",
                "reason": "DUAL_2PC_SUBJECT_INDEX env 필수",
            },
        )

    # Lock 내부에서 state 전이 — race 방지함
    async with app.state.assign_lock:
        current = app.state.registered_group_id

        # 멱등: same group_id → backend 재호출 금지, heartbeat 재생성 금지
        if current == body.group_id:
            return {"status": "already_registered", "groupId": body.group_id}

        superseded: str | None = None
        # 다른 groupId 이미 등록됨 — idle-rebind 자동복구 (Phase 19)
        if current is not None:
            # 측정중이면 라이브 보호 — 409 유지 (DE가 최종 거부권 소유)
            if _is_streaming_for(current):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "group_id_conflict",
                        "current": current,
                        "reason": "measuring",
                    },
                )
            # idle 바인딩 → supersede(해제) 후 아래 신규 등록 경로로 진행함
            _release_binding(app)
            superseded = current

        # 신규 등록 경로: BE /register-dual 호출함
        try:
            await register_to_backend_dual(
                app.state.public_url,
                body.group_id,
                app.state.subject_index,
                app.state.secret_key,
            )
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            # 실패 시 state pending 유지 (재시도 허용함)
            raise HTTPException(
                status_code=502,
                detail={"error": "backend_register_failed", "detail": str(e)},
            )

        # 등록 성공 — state 업데이트 + heartbeat 시작함
        app.state.registered_group_id = body.group_id
        app.state.heartbeat_task = asyncio.create_task(
            start_heartbeat_dual(
                app.state.public_url,
                body.group_id,
                app.state.subject_index,
                app.state.secret_key,
            )
        )

        return {
            "status": "registered",
            "groupId": body.group_id,
            "superseded": superseded,
        }


@router.post("/control/release")
async def release_group(
    body: ReleaseRequest,
    request: Request,
    x_engine_secret: str = Header(alias="X-Engine-Secret"),
):
    """그룹 바인딩 강제 해제함 — 비상 전체해제 / BE supersede 용.

    - group_id 미지정 → 현재 바인딩 무조건 해제 + 해당 그룹 스트림 정지함 (비상)
    - group_id 지정 + current 일치 → 해제 + 스트림 정지함
    - group_id 지정 + current 불일치 → no-op (released=false)

    측정중이어도 해제함 — 비상 도구이므로 orphan core.main도 함께 정지함.

    Raises:
        HTTPException 401 — secret 불일치 시.
    """
    app = request.app
    if x_engine_secret != app.state.secret_key:
        raise HTTPException(status_code=401, detail={"error": "invalid_secret"})

    async with app.state.assign_lock:
        current = app.state.registered_group_id
        target = body.group_id or current

        stopped: list[str] = []
        if target:
            stopped = _stop_streams_for(target)

        released = False
        if current is not None and (body.group_id is None or body.group_id == current):
            _release_binding(app)
            released = True

        return {"released": released, "previous": current, "stopped": stopped}
