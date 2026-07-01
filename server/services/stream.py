"""EEG 스트리밍 프로세스 관리 서비스임"""

import glob
import os
import subprocess
import sys
from pathlib import Path
from typing import IO, Any

# 엔진 루트 디렉토리 (stream.py → server/services/ → server/ → engine root)
_ENGINE_ROOT = str(Path(__file__).resolve().parent.parent.parent)

# core.main 자식 로그 디렉토리 (D7: stdout/stderr를 PIPE 대신 파일로 redirect함)
_LOG_DIR = Path(_ENGINE_ROOT) / "logs"

# 실행 중인 스트리밍 프로세스 추적 dict — key: "{group_id}:{subject_index}"
_processes: dict[str, subprocess.Popen] = {}

# 자식 프로세스별 로그 파일 핸들 추적 dict — 자식 수명 동안 열어둠 (D7)
_log_files: dict[str, IO[str]] = {}


def _make_key(group_id: str, subject_index: int) -> str:
    """프로세스 식별 키 생성함"""
    return f"{group_id}:{subject_index}"


def _close_log(key: str) -> None:
    """key에 연결된 로그 파일 핸들 닫고 정리함 (멱등)"""
    handle = _log_files.pop(key, None)
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def _upload_subject_csv(group_id: str, subject_index: int) -> None:
    """종료된 subject의 CSV를 operator BE로 업로드함 (2-PC 집계).

    Windows에서 proc.terminate()는 TerminateProcess(하드 킬)이라 core.main의
    SIGTERM/on_close 업로드가 실행되지 않음. DE 서버가 종료 직후 직접 업로드해
    수동 중지에도 subject CSV가 operator csv/에 집계되게 함. upload는 멱등 overwrite라
    auto-stop의 on_close 업로드와 중복돼도 무해함.
    """
    from server.config import settings
    from server.services.webhook import upload_csv_to_backend

    # group_id는 glob.escape로 감싸 *,?,[ 메타문자 오매칭 방지함 (CodeRabbit)
    pattern = os.path.join(
        _ENGINE_ROOT,
        "csv",
        f"subject_{subject_index}_{glob.escape(group_id)}_*.csv",
    )
    matches = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not matches:
        print(f"[stop_stream] subject CSV 미발견, 업로드 skip: {pattern}")
        return
    upload_csv_to_backend(
        settings.backend_url, matches[-1], settings.engine_secret_key
    )


def start_stream(group_id: str, subject_index: int) -> dict[str, Any]:
    """core.main을 자식 프로세스로 spawn하여 EEG 스트리밍 시작함

    D7: 자식 stdout/stderr를 `logs/core_{group}_{subject}.log` 파일로 redirect함.
    PIPE 미독취로 인한 버퍼 데드락 및 원격/멀티PC 에러 불가시성 해소함.
    """
    key = _make_key(group_id, subject_index)

    # 이미 실행 중인 프로세스 확인함
    if key in _processes:
        proc = _processes[key]
        if proc.poll() is None:
            raise RuntimeError(f"스트림이 이미 실행 중임: {key} (PID {proc.pid})")
        # 이미 종료된 프로세스는 정리함 (로그 핸들 포함)
        del _processes[key]
        _close_log(key)

    # 로그 파일 준비함 — 디렉토리 보장 후 덮어쓰기 모드로 open함
    _LOG_DIR.mkdir(exist_ok=True)
    log_path = _LOG_DIR / f"core_{group_id}_{subject_index}.log"
    log_file = open(log_path, "w", encoding="utf-8")

    # python -u 로 자식 출력 버퍼링 제거 → 실시간 로그 확보함 (D7)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "core.main", group_id, str(subject_index)],
        env=os.environ.copy(),
        cwd=_ENGINE_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    _processes[key] = proc
    _log_files[key] = log_file
    return {
        "status": "started",
        "pid": proc.pid,
        "key": key,
        "log": str(log_path),
    }


def stop_stream(group_id: str, subject_index: int) -> dict[str, Any]:
    """실행 중인 EEG 스트리밍 프로세스 종료함"""
    key = _make_key(group_id, subject_index)

    if key not in _processes:
        raise KeyError(f"실행 중인 스트림을 찾을 수 없음: {key}")

    proc = _processes[key]

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    del _processes[key]
    _close_log(key)
    # Windows 하드킬로 core.main on_close 업로드가 스킵되므로 DE 서버가 직접 업로드함
    try:
        _upload_subject_csv(group_id, subject_index)
    except Exception as e:  # noqa: BLE001 — 업로드 실패는 stop 흐름 안 깸(soft)
        print(f"[stop_stream] CSV 업로드 오류(무시): {e}")
    return {"status": "stopped", "key": key}


def get_all_status() -> list[dict[str, Any]]:
    """모든 스트리밍 프로세스 상태 조회 및 종료된 프로세스 자동 정리함"""
    result = []
    finished_keys = []

    for key, proc in _processes.items():
        running = proc.poll() is None
        entry = {"key": key, "pid": proc.pid, "running": running}

        if not running:
            entry["returncode"] = proc.returncode
            finished_keys.append(key)

        result.append(entry)

    # 종료된 프로세스 자동 정리함 (로그 핸들 포함)
    for key in finished_keys:
        del _processes[key]
        _close_log(key)

    return result
