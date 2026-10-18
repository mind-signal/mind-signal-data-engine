"""DUAL_2PC 측정 흐름 전체를 HTTP와 Socket.io로 진행하고 판정하는 드라이버임 (OPS-W019).

가짜 엔진 두 개(scripts/replay_data_engine.py)와 실제 백엔드, 프록시, 분석 엔진이
떠 있다는 전제로 PLAN 6.1절 순서를 따름:

  가입과 로그인 3명 → 세션 2개 → pair 2회 → invite-operator, join-as-operator
  → 엔진 등록 확인 → 소켓 join-room → stream:start → 측정 시간 대기
  → stop-all → 분석 결과 폴링 → 판정(PLAN 6.4절) JSON 출력

판정에 실패한 항목이 있으면 종료 코드 1임.

실행 예시:
  python scripts/replay_driver.py --backend-url http://127.0.0.1:5100 \\
      --csv-dir ../csv --duration-sec 200
"""

import argparse
import json
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websocket

# PLAN 6.4절의 임시 기준값임. 첫 실행 결과로 다시 정함
MIN_PAIRED_RATIO = 0.9
MAX_SINGLE_RATIO = 0.1


class SocketRecorder:
    """Socket.io(Engine.IO v4) 이벤트를 받아 모으는 최소 클라이언트임.

    엔진 venv에 python-socketio가 없어 websocket-client로 프로토콜의 필요한 부분만
    구현함: 연결("40"), 이벤트("42"), 응답("43"), ping("2")→pong("3").
    """

    def __init__(self, backend_url: str) -> None:
        """백엔드 URL에서 웹소켓 주소를 만듦.

        Args:
            backend_url: 백엔드 베이스 URL (http 또는 https).
        """
        u = urlparse(backend_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        self._url = f"{scheme}://{u.netloc}/socket.io/?EIO=4&transport=websocket"
        self.events: list[tuple[float, str, object]] = []
        self.acks: list[object] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws: websocket.WebSocket | None = None
        self._thread: threading.Thread | None = None

    def connect(self) -> None:
        """연결하고 수신 스레드를 시작함."""
        self._ws = websocket.create_connection(self._url, timeout=10)
        if not self._ws.recv().startswith("0"):
            raise RuntimeError("Engine.IO open 패킷 미수신")
        self._ws.send("40")
        if not self._ws.recv().startswith("40"):
            raise RuntimeError("Socket.IO connect 응답 미수신")
        self._ws.settimeout(1)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def emit(self, event: str, data: object, ack_id: int | None = None) -> None:
        """이벤트를 보냄.

        Args:
            event: 이벤트 이름.
            data: 이벤트 데이터.
            ack_id: 응답 ID. 주면 서버 응답이 acks에 쌓임.
        """
        assert self._ws is not None
        prefix = "42" if ack_id is None else f"42{ack_id}"
        self._ws.send(prefix + json.dumps([event, data]))

    def _loop(self) -> None:
        """수신 프레임을 이벤트, 응답, ping으로 나눠 처리함. 연결이 끊기면 끝남."""
        assert self._ws is not None
        while not self._stop.is_set():
            try:
                msg = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketException, OSError):
                return
            if msg == "2":
                self._ws.send("3")
            elif msg.startswith("42"):
                name, *rest = json.loads(msg[2:])
                with self._lock:
                    self.events.append((time.time(), name, rest[0] if rest else None))
            elif msg.startswith("43"):
                body = msg[2:].lstrip("0123456789")
                with self._lock:
                    self.acks.append(json.loads(body))

    def named(self, name: str) -> list:
        """받은 이벤트 중 이름이 같은 것의 데이터 목록을 반환함.

        Args:
            name: 이벤트 이름.

        Returns:
            수신 순서대로 정렬된 데이터 목록.
        """
        with self._lock:
            return [data for _, n, data in self.events if n == name]

    def close(self) -> None:
        """수신 스레드를 멈추고 연결을 닫음."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._ws is not None:
            self._ws.close()


def count_csv_rows(csv_dir: Path, group_id: str, subject_index: int) -> int:
    """재생 CSV의 데이터 행 수(머리줄 제외)를 셈.

    Args:
        csv_dir: 재생 CSV가 쌓이는 폴더.
        group_id: 측정 그룹 ID.
        subject_index: 피실험자 번호(1 또는 2).

    Returns:
        가장 최근 파일의 데이터 행 수. 파일이 없으면 0 반환함.
    """
    files = sorted(
        csv_dir.glob(f"subject_{subject_index}_{group_id}_*.csv"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return 0
    with open(files[-1], encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)


def judge(
    pairs: list[dict],
    rows: dict[int, int],
    complete_events: int,
    session_statuses: list[str],
    analysis: dict | None,
    analysis_status_events: list[dict],
    natural: bool = False,
    stop_reasons: list[str | None] | None = None,
    complete_stop_reasons: list[str | None] | None = None,
) -> dict:
    """PLAN 6.4절 판정 4개를 계산함. natural이면 자연 종료 판정 2개를 더함.

    Args:
        pairs: 받은 aligned_pair 데이터 목록.
        rows: subject 번호별 재생 CSV 행 수.
        complete_events: measurement-complete 수신 횟수.
        session_statuses: 그룹의 두 세션 상태.
        analysis: GET /api/analysis/:groupId 의 data. 없으면 None.
        analysis_status_events: analysis-status 수신 목록. SESSION-W008 전에는
            PARTIAL, ABORTED만 오고 그 뒤에는 VALID도 옴.
        natural: 자연 종료 시나리오면 참. stop_reason, tier_event 항목을 더함.
        stop_reasons: 그룹 상태 응답에서 읽은 두 세션의 stopReason. 응답에 필드가
            없으면 그 자리는 None 임.
        complete_stop_reasons: measurement-complete 이벤트의 stopReason (참고값).

    Returns:
        항목별 pass 여부와 수치, 전체 pass를 담은 dict.
    """
    both = sum(1 for p in pairs if p.get("subject_1") and p.get("subject_2"))
    single = len(pairs) - both
    replay_rows = min(rows.values()) if rows else 0
    paired_ratio = both / replay_rows if replay_rows else 0.0
    single_ratio = single / len(pairs) if pairs else 1.0
    # SESSION-W008 전의 BE는 VALID 등급을 알리지 않음. DUAL 분석 결과가 있고
    # PARTIAL/ABORTED 이벤트가 없으면 VALID로 간주함
    tiers = [e.get("tier") for e in analysis_status_events]
    bad_tiers = [t for t in tiers if t in ("PARTIAL", "ABORTED")]
    result = {
        "aligned_pairs": {
            "pass": replay_rows > 0 and paired_ratio >= MIN_PAIRED_RATIO,
            "both": both,
            "replay_rows": rows,
            "ratio": round(paired_ratio, 4),
        },
        "single_emit": {
            "pass": bool(pairs) and single_ratio <= MAX_SINGLE_RATIO,
            "single": single,
            "total": len(pairs),
            "ratio": round(single_ratio, 4),
        },
        "measurement_complete": {
            "pass": complete_events == 1
            and len(session_statuses) == 2
            and all(s == "COMPLETED" for s in session_statuses),
            "events": complete_events,
            "statuses": session_statuses,
        },
        "analysis": {
            "pass": analysis is not None
            and not analysis.get("isBTI")
            and not bad_tiers,
            "received": analysis is not None,
            "isBTI": None if analysis is None else analysis.get("isBTI"),
            "tier_events": tiers,
        },
    }
    if natural:
        reasons = stop_reasons or []
        result["stop_reason"] = {
            "pass": len(reasons) == 2 and all(r == "Natural" for r in reasons),
            "reasons": reasons,
            "complete_event_reasons": complete_stop_reasons or [],
        }
        result["tier_event"] = {"pass": "VALID" in tiers, "tiers": tiers}
    result["pass"] = all(v["pass"] for v in result.values())
    return result


class Driver:
    """백엔드 HTTP 호출을 단계별로 묶음."""

    def __init__(self, backend_url: str) -> None:
        """HTTP 클라이언트를 만듦."""
        self.http = httpx.Client(base_url=backend_url, timeout=30)

    def call(self, method: str, path: str, token: str | None = None, **kw) -> dict:
        """요청을 보내고 JSON을 반환함.

        Args:
            method: HTTP 메서드.
            path: 백엔드 경로.
            token: Bearer JWT. None이면 인증 헤더를 붙이지 않음.
            **kw: httpx 요청 인자(json, params 등).

        Returns:
            응답 JSON. 본문이 없으면 빈 dict 반환함.

        Raises:
            RuntimeError: 응답 코드가 2xx가 아닐 때 본문 일부와 함께 발생함.
        """
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = self.http.request(method, path, headers=headers, **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {path} → {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def user(self, email: str, password: str, name: str) -> str:
        """가입 후 로그인해 JWT를 반환함.

        Args:
            email: 실행마다 고유한 이메일.
            password: 비밀번호.
            name: 표시 이름.

        Returns:
            로그인 응답의 JWT 문자열.
        """
        self.call(
            "POST",
            "/api/auth/signup",
            json={
                "email": email,
                "password": password,
                "passwordConfirm": password,
                "name": name,
            },
        )
        login = self.call(
            "POST", "/api/auth/login", json={"email": email, "password": password}
        )
        return login["token"]


def _wait(predicate, timeout: float, interval: float = 1.0):
    """predicate가 참 값을 낼 때까지 기다림.

    Args:
        predicate: 인자 없이 호출하는 판정 함수.
        timeout: 최대 대기 시간(초).
        interval: 재호출 간격(초).

    Returns:
        predicate의 첫 참 값. 시간 초과 시 None 반환함.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def log(msg: str) -> None:
    """진행 상황을 stderr에 씀. stdout은 판정 JSON 전용임."""
    print(f"[driver] {msg}", file=sys.stderr, flush=True)


def run(args: argparse.Namespace) -> dict:
    """측정 흐름 전체를 실행하고 판정 결과를 반환함.

    Args:
        args: _parse_args 결과.

    Returns:
        groupId, 실행 ID, 판정 dict를 담은 dict.

    Raises:
        RuntimeError: 엔진 등록, join-room, 측정 시작이 실패하거나 HTTP 호출이
            2xx가 아닐 때 발생함.
    """
    d = Driver(args.backend_url)
    run_id = uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(12)

    op = d.user(f"replay-{run_id}-op@example.test", password, "replay-operator")
    subj_a = d.user(f"replay-{run_id}-a@example.test", password, "replay-subject-a")
    subj_b = d.user(f"replay-{run_id}-b@example.test", password, "replay-subject-b")
    log(f"사용자 3명 가입 완료 (run {run_id})")

    s1 = d.call("POST", "/api/sessions", op, json={"experimentMode": "DUAL_2PC"})
    group_id = s1["data"]["groupId"]
    s2 = d.call("POST", "/api/sessions", op, json={"groupId": group_id})
    log(f"groupId={group_id}")
    d.call("POST", f"/api/sessions/{s1['data']['pairingToken']}/pair", subj_a)
    d.call("POST", f"/api/sessions/{s2['data']['pairingToken']}/pair", subj_b)
    invite = d.call("POST", f"/api/sessions/{group_id}/invite-operator", op)
    d.call(
        "POST",
        "/api/sessions/join-as-operator",
        json={"token": invite["data"]["token"]},
    )

    def _registry():
        """엔진 등록 상태를 조회해 두 엔진이 준비됐으면 그 상태를 반환함."""
        s = d.call(
            "GET", "/api/engine/registry-status", op, params={"groupId": group_id}
        )
        return s if s.get("ready") else None

    status = _wait(_registry, args.register_timeout_sec)
    if not status:
        raise RuntimeError(f"엔진 등록 시간 초과 (groupId={group_id})")
    log(f"엔진 등록 확인: {status}")

    sock = SocketRecorder(args.backend_url)
    sock.connect()
    sock.emit("join-room", {"groupId": group_id, "token": op}, ack_id=0)
    if not _wait(lambda: sock.acks, 10, 0.1) or not sock.acks[0][0].get("ok"):
        raise RuntimeError(f"join-room 실패: {sock.acks}")

    d.call("POST", f"/api/measurements/groups/{group_id}/eeg/stream:start", op)
    ready = _wait(
        lambda: sock.named("dual-session-ready") or sock.named("dual-session-failed"),
        args.register_timeout_sec + 30,
    )
    if not ready or sock.named("dual-session-failed"):
        raise RuntimeError(f"측정 시작 실패: {sock.named('dual-session-failed')}")
    if args.natural:
        # 자연 종료: 엔진이 CSV를 올리면 BE가 스스로 끝내야 함. stop-all 안 부름
        log(f"측정 시작. 자연 종료를 최대 {args.duration_sec + 120}초 기다림")
        if not _wait(
            lambda: sock.named("measurement-complete"), args.duration_sec + 120
        ):
            # 시간 초과: 판정은 measurement_complete 실패로 남고, 다음 실행이
            # 막히지 않게 남은 스트림을 정리함
            log("자연 종료 시간 초과. stop-all(ProcessError)로 스트림을 정리함")
            try:
                stop = d.call(
                    "POST",
                    "/api/engine/stream/stop-all",
                    op,
                    json={"groupId": group_id, "stopReason": "ProcessError"},
                )
                log(f"stop-all: {stop}")
            except (RuntimeError, httpx.HTTPError) as e:
                # 정리 실패로 판정까지 잃지 않게 로그만 남기고 계속함
                log(f"stop-all 실패: {e}")
    else:
        log(f"측정 시작. {args.duration_sec}초 대기함")
        time.sleep(args.duration_sec)
        stop = d.call(
            "POST",
            "/api/engine/stream/stop-all",
            op,
            json={"groupId": group_id, "stopReason": "Natural"},
        )
        log(f"stop-all: {stop}")

    def _analysis():
        """분석 결과가 있으면 data를, analysis-status 이벤트가 왔으면 표지를 반환함."""
        r = d.http.get(
            f"/api/analysis/{group_id}", headers={"Authorization": f"Bearer {op}"}
        )
        if r.status_code == 200:
            return r.json()["data"]
        tiers = [e.get("tier") for e in sock.named("analysis-status")]
        return ("PARTIAL" in tiers or "ABORTED" in tiers) and {"_aborted": True}

    # 자연 종료가 안 됐으면 분석도 시작되지 않으므로 폴링을 건너뜀
    ended = not args.natural or sock.named("measurement-complete")
    analysis = _wait(_analysis, args.analysis_timeout_sec, 2.0) if ended else None
    if analysis and analysis.get("_aborted"):
        analysis = None
    _wait(lambda: sock.named("measurement-complete"), 10)
    time.sleep(2)  # 중복 measurement-complete 수신 여부를 보려고 잠시 더 받음
    sessions = d.call("GET", f"/api/sessions/group/{group_id}/status", op)
    sock.close()

    csv_dir = Path(args.csv_dir)
    rows = {i: count_csv_rows(csv_dir, group_id, i) for i in (1, 2)}
    group_sessions = sessions["data"]["sessions"]
    verdict = judge(
        sock.named("aligned_pair"),
        rows,
        len(sock.named("measurement-complete")),
        [s["status"] for s in group_sessions],
        analysis,
        sock.named("analysis-status"),
        natural=args.natural,
        stop_reasons=[s.get("stopReason") for s in group_sessions],
        complete_stop_reasons=[
            e.get("stopReason") for e in sock.named("measurement-complete")
        ],
    )
    return {"groupId": group_id, "run": run_id, "verdict": verdict}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 해석해 반환함."""
    p = argparse.ArgumentParser(
        description="DUAL_2PC 측정 흐름을 진행하고 PLAN 6.4절로 판정함 (OPS-W019)"
    )
    p.add_argument("--backend-url", default="http://127.0.0.1:5100")
    p.add_argument(
        "--csv-dir",
        required=True,
        help="재생 CSV가 쌓이는 폴더 (행 수 집계용, 보통 mind-signal/csv)",
    )
    p.add_argument("--duration-sec", type=float, default=200)
    p.add_argument("--register-timeout-sec", type=float, default=60)
    p.add_argument("--analysis-timeout-sec", type=float, default=180)
    p.add_argument(
        "--natural",
        action="store_true",
        help="stop-all 없이 자연 종료를 기다리고 stop_reason, tier_event를 판정에 더함 "
        "(가짜 엔진 --natural-end와 함께 씀)",
    )
    return p.parse_args(argv)


def main() -> None:
    """CLI 진입점임. 판정 JSON을 stdout에 쓰고 실패 시 종료 코드 1임."""
    try:
        result = run(_parse_args())
    except (RuntimeError, httpx.HTTPError, websocket.WebSocketException) as e:
        print(json.dumps({"error": str(e), "verdict": {"pass": False}}))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["verdict"]["pass"] else 1)


if __name__ == "__main__":
    main()
