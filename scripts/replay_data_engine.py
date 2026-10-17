"""실측 CSV를 재생해 실제 엔진처럼 프록시로 샘플을 보내는 가짜 엔진 서버임 (OPS-W019).

헤드셋과 Cortex 없이 DUAL_2PC 흐름(그룹 배정, 샘플 전송, 측정 종료, CSV 업로드)을
재현하기 위함임. 실제 엔진(`server/`)의 HTTP 계약을 그대로 따름:

- 기동 시 프록시 `POST /register` 후 주기 재등록 (`server/app.py`)
- `GET /health` (`server/routes/health.py`)
- `POST /control/assign-group` → BE `register-dual` (`server/routes/control.py`)
- `POST /api/stream/start` → CSV 행을 timestamp 간격대로 `/ingest/sample`에 전송
- `POST /api/stream/stop` → 응답 전에 BE `csv-upload` 완료 (`server/services/stream.py`)

시크릿은 환경 변수 `ENGINE_SECRET_KEY`에서만 읽음.

실행 예시:
  ENGINE_SECRET_KEY=... python scripts/replay_data_engine.py --subject-index 1 \\
      --port 5011 --csv ../csv/subject_1_xxx.csv \\
      --proxy-url http://127.0.0.1:5050 --backend-url http://127.0.0.1:5100
"""

import argparse
import asyncio
import csv
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

# scripts/ 직접 실행 시에도 엔진 패키지를 import 하도록 레포 루트를 경로에 추가함
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.services.proxy_client import get_shared_client  # noqa: E402
from server.services.session_csv import (  # noqa: E402
    METRIC_COLUMNS,
    SESSION_CSV_DIR,
    WAVE_COLUMNS,
)

# core/streamer.py의 TARGET_EEG_CHANNELS와 같은 순서임. streamer는 Redis와 SDK를
# import 하므로 끌어오지 않고 같은 값을 둠
CHANNELS: tuple[str, ...] = ("AF3", "T7", "Pz", "T8", "AF4")

# 실제 엔진 CSV의 열 순서임 (core/streamer.py 헤더와 같음)
CSV_HEADER: list[str] = [
    "timestamp",
    *WAVE_COLUMNS,
    *METRIC_COLUMNS,
    *[f"{ch}_{band}" for ch in CHANNELS for band in WAVE_COLUMNS],
]

# timestamp 해석 실패나 역행 시 쓰는 기본 재생 간격(초)임
DEFAULT_INTERVAL_SEC = 1.0


def _to_float(raw: str | None) -> float | None:
    """CSV 칸 하나를 유한 실수로 바꿈. 빈 값이나 비유한 값은 None 반환함."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_ts(raw: str | None) -> datetime | None:
    """CSV timestamp 칸을 datetime으로 바꿈. 실패 시 None 반환함."""
    try:
        return datetime.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def replay_intervals(timestamps: list[str]) -> list[float]:
    """연속 행 사이의 재생 대기 시간(초) 목록을 계산함.

    Args:
        timestamps: CSV timestamp 칸 문자열 목록.

    Returns:
        길이 len(timestamps)의 목록. i번째 값은 i번째 행을 보내기 전 대기 시간이며
        첫 행은 0임. 해석 실패나 0 이하 간격은 DEFAULT_INTERVAL_SEC로 대체함.
    """
    parsed = [_parse_ts(t) for t in timestamps]
    intervals = [0.0] * len(parsed)
    for i in range(1, len(parsed)):
        prev, cur = parsed[i - 1], parsed[i]
        gap = (cur - prev).total_seconds() if prev and cur else 0.0
        intervals[i] = gap if gap > 0 else DEFAULT_INTERVAL_SEC
    return intervals


def row_to_sample(
    row: dict[str, str], last_metrics: dict[str, float]
) -> tuple[dict[str, float], dict[str, float]] | None:
    """CSV 행 하나를 envelope의 payload(5대역)와 metrics(6지표)로 바꿈.

    대역 값이 하나라도 비었거나 비유한이면 실제 엔진처럼 그 행을 버림(None 반환).
    지표 값이 비었거나 비유한이면 실제 엔진의 latest_met처럼 직전 값을 유지함.
    BE와 프록시의 zod 스키마가 빈 값과 null을 거부하기 때문임.

    Args:
        row: csv.DictReader 행.
        last_metrics: 직전 지표 값. 첫 행 전에는 전부 0 임.

    Returns:
        (payload, metrics) 튜플 또는 버릴 행이면 None.
    """
    payload: dict[str, float] = {}
    for band in WAVE_COLUMNS:
        value = _to_float(row.get(band))
        if value is None:
            return None
        payload[band] = value
    metrics = {}
    for name in METRIC_COLUMNS:
        value = _to_float(row.get(name))
        metrics[name] = last_metrics[name] if value is None else value
    return payload, metrics


def csv_out_row(
    row: dict[str, str],
    payload: dict[str, float],
    metrics: dict[str, float],
    now: datetime,
) -> list:
    """재생 CSV에 쓸 행을 실제 엔진 열 순서로 만듦. timestamp는 전송 시각임.

    Args:
        row: 원본 csv.DictReader 행. 채널별 대역 값을 여기서 가져옴.
        payload: 5대역 값.
        metrics: 6지표 값.
        now: 행의 timestamp로 쓸 전송 시각.

    Returns:
        CSV_HEADER 순서의 값 목록. 비었거나 비유한인 채널 값은 빈 칸임.
    """
    return [
        now.strftime("%Y-%m-%d %H:%M:%S.%f"),
        *[payload[b] for b in WAVE_COLUMNS],
        *[metrics[m] for m in METRIC_COLUMNS],
        *[
            "" if _to_float(row.get(col)) is None else row[col]
            for col in CSV_HEADER[1 + len(WAVE_COLUMNS) + len(METRIC_COLUMNS) :]
        ],
    ]


def build_envelope(
    group_id: str,
    subject_idx: int,
    seq: int,
    payload: dict[str, float],
    metrics: dict[str, float],
) -> dict:
    """프록시 /ingest/sample envelope을 만듦 (proxy_client.post_sample과 같은 본문).

    de_ts_ns는 여기서 한 번만 찍음. 실제 엔진처럼 재시도와 재전송이 같은
    envelope(같은 seq와 de_ts_ns)을 그대로 다시 보내게 하기 위함임.

    Args:
        group_id: 측정 그룹 ID.
        subject_idx: 피실험자 번호.
        seq: 엔진 단위 단조 증가 시퀀스 번호.
        payload: 5대역 값.
        metrics: 6지표 값.

    Returns:
        envelope dict.
    """
    return {
        "group_id": group_id,
        "subject_idx": subject_idx,
        "de_ts_ns": str(time.monotonic_ns()),
        "seq": seq,
        "payload": payload,
        "sync_meta": {"de_clock_domain": "monotonic_ns"},
        "metrics": dict(metrics),
    }


def _post_envelope(
    proxy_url: str,
    secret: str,
    body: dict,
    backoffs_sec: tuple[float, ...] = (0.1, 0.2),
) -> None:
    """envelope을 /ingest/sample로 보냄. post_sample처럼 같은 본문으로 재시도함.

    Args:
        proxy_url: 프록시 베이스 URL.
        secret: X-Engine-Secret 헤더 값.
        body: build_envelope가 만든 envelope.
        backoffs_sec: 재시도 전 대기 시간 목록. 총 시도는 1 + 길이임.

    Raises:
        httpx.HTTPError: 재시도를 다 쓰고도 실패하면 마지막 오류를 냄.
    """
    last: httpx.HTTPError | None = None
    for wait in (0.0, *backoffs_sec):
        time.sleep(wait)
        try:
            response = get_shared_client().post(
                f"{proxy_url}/ingest/sample",
                headers={"X-Engine-Secret": secret},
                json=body,
            )
            response.raise_for_status()
            return
        except httpx.HTTPError as e:
            last = e
    assert last is not None
    raise last


@dataclass
class ReplayConfig:
    """가짜 엔진 실행 설정임."""

    subject_index: int
    port: int
    csv_path: Path
    proxy_url: str
    backend_url: str
    secret: str
    public_url: str
    csv_dir: Path = SESSION_CSV_DIR
    delay_ms: int = 0
    resend_every: int = 0
    natural_end: bool = False
    max_rows: int = 0
    reregister_sec: float = 20.0


class AssignGroupRequest(BaseModel):
    """/control/assign-group 요청 바디임 (실제 엔진과 같음)"""

    group_id: str = Field(..., min_length=1)


class StreamRequest(BaseModel):
    """/api/stream/start, /api/stream/stop 요청 바디임 (실제 엔진과 같음)"""

    group_id: str
    subject_index: int


async def _register_to_proxy(cfg: ReplayConfig) -> None:
    """프록시에 subject_idx와 de_url 등록함 (webhook.register_to_proxy와 같은 계약)."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{cfg.proxy_url}/register",
            headers={"X-Engine-Secret": cfg.secret},
            json={"subject_idx": cfg.subject_index, "de_url": cfg.public_url},
        )
        response.raise_for_status()
    print(f"proxy 등록 완료: subject_idx={cfg.subject_index} url={cfg.public_url}")


async def _reregister_loop(cfg: ReplayConfig) -> None:
    """기동 시 등록하고 주기적으로 재등록함. 실패는 다음 주기에 재시도함."""
    while True:
        try:
            await _register_to_proxy(cfg)
        except httpx.HTTPError as e:
            print(f"[WARN] proxy 등록 실패 (non-fatal): {e}")
        await asyncio.sleep(cfg.reregister_sec)


async def _register_dual(cfg: ReplayConfig, group_id: str) -> None:
    """BE register-dual 호출함 (webhook.register_to_backend_dual과 같은 계약)."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{cfg.backend_url}/api/engine/register-dual",
            json={
                "groupId": group_id,
                "subjectIndex": cfg.subject_index,
                "engineUrl": cfg.public_url,
                "secretKey": cfg.secret,
            },
        )
        response.raise_for_status()
    print(f"DUAL_2PC register success: groupId={group_id}")


def _upload_csv(cfg: ReplayConfig, path: Path) -> bool:
    """재생 CSV를 BE csv-upload로 보냄 (webhook.upload_csv_to_backend와 같은 계약)."""
    try:
        response = httpx.post(
            f"{cfg.backend_url}/api/engine/csv-upload",
            params={"filename": path.name},
            headers={"X-Engine-Secret": cfg.secret, "Content-Type": "text/csv"},
            content=path.read_bytes(),
            timeout=10.0,
        )
        response.raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        print(f"[WARN] csv-upload failed (soft): {e}")
        return False
    print(f"[csv-upload] {path.name} 업로드 완료")
    return True


async def _replay(cfg: ReplayConfig, group_id: str, out_path: Path, stats: dict):
    """CSV 행을 timestamp 간격대로 프록시에 보내고 재생 CSV에 기록함."""
    with open(cfg.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if cfg.max_rows:
        rows = rows[: cfg.max_rows]
    intervals = replay_intervals([r.get("timestamp", "") for r in rows])
    metrics = {m: 0.0 for m in METRIC_COLUMNS}
    loop = asyncio.get_running_loop()
    # 지연 주입은 누적이 아니라 일정한 시차로 적용함 (한 PC가 늦는 상황 재현)
    t0 = loop.time() + cfg.delay_ms / 1000
    offset = 0.0
    seq = 0
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(CSV_HEADER)
        for row, gap in zip(rows, intervals):
            offset += gap
            await asyncio.sleep(max(0.0, t0 + offset - loop.time()))
            sample = row_to_sample(row, metrics)
            if sample is None:
                stats["skipped"] += 1
                continue
            payload, metrics = sample
            writer.writerow(csv_out_row(row, payload, metrics, datetime.now()))
            out.flush()
            seq += 1
            body = build_envelope(group_id, cfg.subject_index, seq, payload, metrics)
            # 재전송은 실제 엔진 재시도처럼 같은 envelope을 그대로 다시 보냄
            sends = 2 if cfg.resend_every and seq % cfg.resend_every == 0 else 1
            for _ in range(sends):
                try:
                    await asyncio.to_thread(
                        _post_envelope, cfg.proxy_url, cfg.secret, body
                    )
                    stats["sent"] += 1
                except Exception as e:  # noqa: BLE001 — 재생은 실패해도 계속함
                    stats["failed"] += 1
                    print(f"[WARN] sample seq={seq} 전송 실패: {e}")
            stats["rows"] = seq
    print(f"[replay] 재생 끝남: {stats}")
    if cfg.natural_end:
        # 실제 엔진 core/streamer.py on_close처럼 CSV를 닫은 직후 BE로 올림
        stats["natural_uploaded"] = await asyncio.to_thread(_upload_csv, cfg, out_path)


def create_app(cfg: ReplayConfig) -> FastAPI:
    """가짜 엔진 FastAPI 앱을 만듦.

    Args:
        cfg: 실행 설정.

    Returns:
        FastAPI 앱.
    """
    streams: dict[str, dict] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """기동 시 상태 초기화와 프록시 재등록 태스크를 시작하고 종료 시 정리함."""
        app.state.registered_group_id = None
        app.state.assign_lock = asyncio.Lock()
        task = asyncio.create_task(_reregister_loop(cfg))
        yield
        task.cancel()
        for s in streams.values():
            s["task"].cancel()

    app = FastAPI(title="Mind Signal Replay Data Engine", lifespan=lifespan)

    def _reap() -> None:
        """끝난 재생을 목록에서 지움.

        실제 엔진 get_all_status가 끝난 프로세스를 정리하는 것과 같음. 그래서
        재생이 끝난 뒤 /health나 assign-group이 먼저 불리면 stop은 404가 됨.
        """
        for key in [k for k, s in streams.items() if s["task"].done()]:
            del streams[key]

    def _streaming(group_id: str | None = None) -> bool:
        """재생 중인 스트림이 있는지 반환함. group_id를 주면 그 그룹만 봄."""
        _reap()
        return any(
            not s["task"].done()
            for key, s in streams.items()
            if group_id is None or key.startswith(f"{group_id}:")
        )

    def _check_secret(secret: str) -> None:
        """stream 계열 시크릿 불일치 시 실제 엔진처럼 403 발생시킴."""
        if secret != cfg.secret:
            raise HTTPException(
                status_code=403, detail="인증 실패: 유효하지 않은 시크릿 키임"
            )

    @app.get("/health")
    async def health(request: Request):
        """실제 엔진 /health와 같은 형태의 상태 반환함."""
        return {
            "status": "ok",
            "service": "mind-signal-data-engine",
            "subject_index": cfg.subject_index,
            "registered_group_id": request.app.state.registered_group_id,
            "streaming": _streaming(),
        }

    @app.post("/control/assign-group")
    async def assign_group(
        body: AssignGroupRequest,
        request: Request,
        x_engine_secret: str = Header(alias="X-Engine-Secret"),
    ):
        """groupId를 주입하고 BE register-dual을 호출함.

        실제 엔진 `server/routes/control.py`의 계약을 따름: 시크릿 불일치 401
        invalid_secret, 같은 groupId 재호출 already_registered, 재생 중 다른
        groupId 409 group_id_conflict, BE 실패 502 backend_register_failed.

        Args:
            body: group_id를 담은 요청 바디.
            request: 앱 상태 접근용 요청 객체.
            x_engine_secret: X-Engine-Secret 헤더 값.

        Returns:
            status와 groupId를 담은 dict.
        """
        state = request.app.state
        if x_engine_secret != cfg.secret:
            raise HTTPException(status_code=401, detail={"error": "invalid_secret"})
        async with state.assign_lock:
            current = state.registered_group_id
            if current == body.group_id:
                return {"status": "already_registered", "groupId": body.group_id}
            if current is not None and _streaming(current):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "group_id_conflict",
                        "current": current,
                        "reason": "measuring",
                    },
                )
            try:
                await _register_dual(cfg, body.group_id)
            except httpx.HTTPError as e:
                raise HTTPException(
                    status_code=502,
                    detail={"error": "backend_register_failed", "detail": str(e)},
                )
            state.registered_group_id = body.group_id
            return {
                "status": "registered",
                "groupId": body.group_id,
                "superseded": current,
            }

    @app.post("/api/stream/start")
    async def stream_start(
        body: StreamRequest, x_engine_secret: str = Header(alias="X-Engine-Secret")
    ):
        """CSV 재생 태스크를 시작함.

        Args:
            body: group_id와 subject_index를 담은 요청 바디.
            x_engine_secret: X-Engine-Secret 헤더 값.

        Returns:
            status, key, 재생 CSV 경로를 담은 dict.
        """
        _check_secret(x_engine_secret)
        key = f"{body.group_id}:{body.subject_index}"
        if key in streams and not streams[key]["task"].done():
            raise HTTPException(
                status_code=409, detail=f"스트림이 이미 실행 중임: {key}"
            )
        cfg.csv_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = (
            cfg.csv_dir / f"subject_{body.subject_index}_{body.group_id}_{stamp}.csv"
        )
        stats = {"rows": 0, "sent": 0, "failed": 0, "skipped": 0}
        task = asyncio.create_task(_replay(cfg, body.group_id, out_path, stats))
        streams[key] = {"task": task, "path": out_path, "stats": stats}
        return {"status": "started", "key": key, "csv": str(out_path)}

    @app.post("/api/stream/stop")
    async def stream_stop(
        body: StreamRequest, x_engine_secret: str = Header(alias="X-Engine-Secret")
    ):
        """재생을 멈추고 응답 전에 재생 CSV를 BE로 업로드함.

        실제 엔진 stop_stream처럼 재생이 이미 끝났어도 목록에 남아 있으면 다시
        업로드함(BE 저장은 덮어쓰기라 멱등). 목록에서 정리됐으면 404 임.

        Args:
            body: group_id와 subject_index를 담은 요청 바디.
            x_engine_secret: X-Engine-Secret 헤더 값.

        Returns:
            status, key, 업로드 성공 여부, 재생 통계를 담은 dict.
        """
        _check_secret(x_engine_secret)
        key = f"{body.group_id}:{body.subject_index}"
        if key not in streams:
            raise HTTPException(
                status_code=404, detail=f"실행 중인 스트림을 찾을 수 없음: {key}"
            )
        s = streams.pop(key)
        s["task"].cancel()
        # 재생 태스크가 CSV를 닫을 때까지 기다림. 재생 중 예외는 결과로만 받음
        await asyncio.gather(s["task"], return_exceptions=True)
        # 실제 엔진처럼 응답 전에 업로드를 끝냄
        uploaded = await asyncio.to_thread(_upload_csv, cfg, s["path"])
        return {"status": "stopped", "key": key, "uploaded": uploaded, **s["stats"]}

    return app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 해석해 반환함."""
    p = argparse.ArgumentParser(
        description="실측 CSV를 재생해 프록시로 샘플을 보내는 가짜 엔진 (OPS-W019)"
    )
    p.add_argument("--subject-index", type=int, choices=(1, 2), required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--csv", type=Path, required=True, help="재생할 실측 CSV 경로")
    p.add_argument("--proxy-url", default="http://127.0.0.1:5050")
    p.add_argument("--backend-url", default="http://127.0.0.1:5100")
    p.add_argument(
        "--public-url",
        default=None,
        help="프록시에 등록할 이 엔진의 URL (기본 http://127.0.0.1:{port})",
    )
    p.add_argument(
        "--csv-dir",
        type=Path,
        default=SESSION_CSV_DIR,
        help=f"재생 CSV를 쓸 폴더 (기본 {SESSION_CSV_DIR})",
    )
    p.add_argument("--delay-ms", type=int, default=0, help="전송마다 더할 지연(ms)")
    p.add_argument(
        "--resend-every",
        type=int,
        default=0,
        help="N행마다 같은 행을 한 번 더 보냄 (0=끔)",
    )
    p.add_argument(
        "--natural-end",
        action="store_true",
        help="CSV를 다 보내면 실제 엔진 on_close처럼 CSV를 BE로 올림 (자연 종료)",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="재생할 CSV 행 수 상한 (0=전부). 등급 VALID 판정에는 200 이상",
    )
    return p.parse_args(argv)


def main() -> None:
    """CLI 진입점임."""
    args = _parse_args()
    secret = os.environ.get("ENGINE_SECRET_KEY", "")
    if not secret:
        sys.exit("ENGINE_SECRET_KEY 환경 변수가 비어 있음")
    if not args.csv.is_file():
        sys.exit(f"CSV 파일 없음: {args.csv}")
    cfg = ReplayConfig(
        subject_index=args.subject_index,
        port=args.port,
        csv_path=args.csv,
        proxy_url=args.proxy_url.rstrip("/"),
        backend_url=args.backend_url.rstrip("/"),
        secret=secret,
        public_url=args.public_url or f"http://127.0.0.1:{args.port}",
        csv_dir=args.csv_dir,
        delay_ms=args.delay_ms,
        resend_every=args.resend_every,
        natural_end=args.natural_end,
        max_rows=args.max_rows,
    )
    uvicorn.run(create_app(cfg), host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
