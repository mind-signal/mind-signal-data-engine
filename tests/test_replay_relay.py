"""중계기(scripts/replay/relay.js) 동작 시험 (OPS-W019 T7).

relay.js는 백엔드 저장소의 socket.io를 NODE_PATH로 씀. node나 그 모듈이 없는
환경에서는 건너뜀.
"""

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_NODE_PATH = os.environ.get(
    "REPLAY_NODE_PATH",
    str(Path.home() / "Team-project/mind-signal/mind-signal-backend/node_modules"),
)


def _free_port() -> int:
    """비어 있는 TCP 포트 하나를 반환함."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(
    shutil.which("node") is None
    or not (Path(_NODE_PATH) / "socket.io-client").is_dir(),
    reason="node 또는 백엔드 node_modules의 socket.io 없음",
)
def test_relay_passes_http_acks_delay_and_duplicates_same_frame():
    """HTTP 통과, 시크릿 거부, subject 지연, --dup-every 같은 프레임 중복을 확인함."""
    result = subprocess.run(
        [
            "node",
            str(_ROOT / "tests" / "replay_relay_check.js"),
            str(_ROOT / "scripts" / "replay" / "relay.js"),
            str(_free_port()),
            str(_free_port()),
        ],
        env={**os.environ, "NODE_PATH": _NODE_PATH},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "RELAY_CHECK_OK" in result.stdout, result.stdout + result.stderr
