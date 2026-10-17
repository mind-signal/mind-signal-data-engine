"""재생 가짜 엔진(scripts/replay_data_engine.py)의 행 변환과 재생 간격 테스트 (OPS-W019)."""

import asyncio
import csv
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "replay_data_engine.py"
SECRET = "test-secret"
GROUP_ID = "6a72db3c2d01f2ce5c690b19"
PROXY = "http://proxy.test"
BACKEND = "http://backend.test"


def _load() -> Any:
    """scripts/ 는 패키지가 아니라 파일 경로로 직접 로드함."""
    spec = importlib.util.spec_from_file_location("replay_data_engine", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_data_engine"] = module
    spec.loader.exec_module(module)
    return module


rde = _load()


def _row(ts: str, **over: str) -> dict[str, str]:
    """합성 CSV 행 하나 생성함. 대역 1.0, 지표 0.5, 채널 값 2.0 기본임."""
    row = {"timestamp": ts}
    row.update({b: "1.0" for b in rde.WAVE_COLUMNS})
    row.update({m: "0.5" for m in rde.METRIC_COLUMNS})
    row.update({c: "2.0" for c in rde.CSV_HEADER[12:]})
    row.update(over)
    return row


def _write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    """합성 행을 실제 엔진 머리줄로 CSV 파일에 쓰고 경로를 반환함."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rde.CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _cfg(tmp_path: Path, src: Path, **over: Any) -> Any:
    """시험용 ReplayConfig를 만듦. over로 필드를 바꿈."""
    kwargs = dict(
        subject_index=2,
        port=5012,
        csv_path=src,
        proxy_url=PROXY,
        backend_url=BACKEND,
        secret=SECRET,
        public_url="http://127.0.0.1:5012",
        csv_dir=tmp_path / "out",
    )
    kwargs.update(over)
    return rde.ReplayConfig(**kwargs)


def test_header_matches_engine_column_order():
    """재생 CSV 머리줄이 core/streamer.py 열 순서와 같은지 확인함."""
    assert rde.CSV_HEADER[:12] == [
        "timestamp",
        "delta",
        "theta",
        "alpha",
        "beta",
        "gamma",
        "focus",
        "engagement",
        "interest",
        "excitement",
        "stress",
        "relaxation",
    ]
    assert len(rde.CSV_HEADER) == 37
    assert rde.CSV_HEADER[12] == "AF3_delta"
    assert rde.CSV_HEADER[-1] == "AF4_gamma"


def test_empty_metric_keeps_previous_value():
    """빈 지표나 비유한 지표가 직전 값으로 채워지는지 확인함."""
    zeros = {m: 0 for m in rde.METRIC_COLUMNS}
    _, first = rde.row_to_sample(_row("t", focus="", stress="nan"), zeros)
    assert first["focus"] == 0 and first["stress"] == 0
    assert first["engagement"] == 0.5
    _, second = rde.row_to_sample(_row("t", focus="", relaxation="0.9"), first)
    assert second["focus"] == 0
    assert second["relaxation"] == 0.9


@pytest.mark.parametrize("bad", ["", "nan", "inf", "x"])
def test_bad_band_value_drops_row(bad: str):
    """대역 값이 비었거나 비유한이면 행을 버리는지 확인함."""
    zeros = {m: 0 for m in rde.METRIC_COLUMNS}
    assert rde.row_to_sample(_row("t", alpha=bad), zeros) is None


def test_replay_intervals_follow_timestamps_with_fallback():
    """재생 간격이 timestamp 차이를 따르고 해석 실패와 역행은 1초로 대체되는지 확인함."""
    got = rde.replay_intervals(
        [
            "2026-09-07 10:00:00.000000",
            "2026-09-07 10:00:01.500000",
            "garbage",
            "2026-09-07 10:00:04.000000",
            "2026-09-07 10:00:03.000000",
        ]
    )
    assert got == [0.0, 1.5, 1.0, 1.0, 1.0]


def test_replay_posts_envelopes_and_writes_engine_csv(
    tmp_path: Path, httpx_mock: HTTPXMock
):
    """재생이 envelope 계약대로 전송하고 재전송과 행 버림을 반영해 CSV를 쓰는지 확인함."""
    httpx_mock.add_response(url=f"{PROXY}/ingest/sample", is_reusable=True)
    src = _write_csv(
        tmp_path / "in.csv",
        [
            _row("2026-09-07 10:00:00.00", focus=""),
            _row("2026-09-07 10:00:00.01", gamma=""),
            _row("2026-09-07 10:00:00.02", AF3_alpha="nan"),
        ],
    )
    cfg = _cfg(tmp_path, src, resend_every=2)
    cfg.csv_dir.mkdir()
    out = cfg.csv_dir / "subject_2_x.csv"
    stats = {"rows": 0, "sent": 0, "failed": 0, "skipped": 0}
    asyncio.run(rde._replay(cfg, GROUP_ID, out, stats))

    assert stats == {"rows": 2, "sent": 3, "failed": 0, "skipped": 1}
    bodies = [json.loads(r.content) for r in httpx_mock.get_requests()]
    assert [b["seq"] for b in bodies] == [1, 2, 2]
    first = bodies[0]
    assert first["group_id"] == GROUP_ID and first["subject_idx"] == 2
    assert first["de_ts_ns"].isdigit()
    assert set(first["payload"]) == set(rde.WAVE_COLUMNS)
    assert first["metrics"]["focus"] == 0
    assert first["sync_meta"] == {"de_clock_domain": "monotonic_ns"}
    for r in httpx_mock.get_requests():
        assert r.headers["X-Engine-Secret"] == SECRET

    with open(out, newline="", encoding="utf-8") as f:
        written = list(csv.reader(f))
    assert written[0] == rde.CSV_HEADER
    assert len(written) == 3
    assert written[2][rde.CSV_HEADER.index("AF3_alpha")] == ""


def test_assign_group_contract(tmp_path: Path, httpx_mock: HTTPXMock):
    """assign-group의 401, registered, already_registered 계약과 /health 반영을 확인함."""
    httpx_mock.add_response(url=f"{PROXY}/register", is_reusable=True, is_optional=True)
    httpx_mock.add_response(url=f"{BACKEND}/api/engine/register-dual")
    app = rde.create_app(_cfg(tmp_path, tmp_path / "in.csv"))
    with TestClient(app) as client:
        bad = client.post(
            "/control/assign-group",
            json={"group_id": GROUP_ID},
            headers={"X-Engine-Secret": "wrong"},
        )
        assert bad.status_code == 401
        assert bad.json() == {"detail": {"error": "invalid_secret"}}
        ok = client.post(
            "/control/assign-group",
            json={"group_id": GROUP_ID},
            headers={"X-Engine-Secret": SECRET},
        )
        assert ok.json()["status"] == "registered"
        again = client.post(
            "/control/assign-group",
            json={"group_id": GROUP_ID},
            headers={"X-Engine-Secret": SECRET},
        )
        assert again.json()["status"] == "already_registered"
        health = client.get("/health").json()
        assert health["subject_index"] == 2
        assert health["registered_group_id"] == GROUP_ID


def test_stop_uploads_csv_before_responding(tmp_path: Path, httpx_mock: HTTPXMock):
    """stop 응답 전에 재생 CSV가 csv-upload 계약대로 올라가는지 확인함."""
    httpx_mock.add_response(url=f"{PROXY}/register", is_reusable=True, is_optional=True)
    httpx_mock.add_response(url=f"{PROXY}/ingest/sample", is_reusable=True)
    httpx_mock.add_response(url=re.compile(rf"{BACKEND}/api/engine/csv-upload.*"))
    src = _write_csv(tmp_path / "in.csv", [_row("2026-09-07 10:00:00")])
    app = rde.create_app(_cfg(tmp_path, src))
    body = {"group_id": GROUP_ID, "subject_index": 2}
    headers = {"X-Engine-Secret": SECRET}
    with TestClient(app) as client:
        assert (
            client.post("/api/stream/stop", json=body, headers=headers).status_code
            == 404
        )
        assert (
            client.post("/api/stream/start", json=body, headers=headers).status_code
            == 200
        )
        time.sleep(0.3)  # 재생 태스크가 첫 행을 쓰도록 기다림
        stopped = client.post("/api/stream/stop", json=body, headers=headers).json()
    assert stopped["status"] == "stopped"
    uploads = [
        r for r in httpx_mock.get_requests() if r.url.path == "/api/engine/csv-upload"
    ]
    assert len(uploads) == 1
    assert uploads[0].url.params["filename"].startswith(f"subject_2_{GROUP_ID}_")
    assert uploads[0].headers["Content-Type"] == "text/csv"
    assert uploads[0].content.startswith(b"timestamp,delta")


def test_natural_end_uploads_once_after_max_rows(tmp_path: Path, httpx_mock: HTTPXMock):
    """--natural-end는 max_rows만큼 보낸 뒤 CSV를 한 번 올리는지 확인함."""
    httpx_mock.add_response(url=f"{PROXY}/ingest/sample", is_reusable=True)
    httpx_mock.add_response(url=re.compile(rf"{BACKEND}/api/engine/csv-upload.*"))
    src = _write_csv(
        tmp_path / "in.csv",
        [_row(f"2026-09-07 10:00:00.0{i}") for i in range(5)],
    )
    cfg = _cfg(tmp_path, src, natural_end=True, max_rows=3)
    cfg.csv_dir.mkdir()
    out = cfg.csv_dir / f"subject_2_{GROUP_ID}_20261018_100000.csv"
    stats = {"rows": 0, "sent": 0, "failed": 0, "skipped": 0}
    asyncio.run(rde._replay(cfg, GROUP_ID, out, stats))

    assert stats["rows"] == 3 and stats["natural_uploaded"] is True
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/ingest/sample") == 3
    assert paths[-1] == "/api/engine/csv-upload"


def test_stop_after_natural_end_matches_real_engine(
    tmp_path: Path, httpx_mock: HTTPXMock
):
    """자연 종료 뒤 stop은 실제 엔진처럼 다시 올리고, /health 정리 뒤에는 404인지 확인함."""
    httpx_mock.add_response(url=f"{PROXY}/register", is_reusable=True, is_optional=True)
    httpx_mock.add_response(url=f"{PROXY}/ingest/sample", is_reusable=True)
    httpx_mock.add_response(
        url=re.compile(rf"{BACKEND}/api/engine/csv-upload.*"), is_reusable=True
    )
    src = _write_csv(tmp_path / "in.csv", [_row("2026-09-07 10:00:00")])
    app = rde.create_app(_cfg(tmp_path, src, natural_end=True))
    body = {"group_id": GROUP_ID, "subject_index": 2}
    headers = {"X-Engine-Secret": SECRET}

    def uploads() -> int:
        """지금까지 csv-upload 요청 수를 셈."""
        return sum(
            r.url.path == "/api/engine/csv-upload" for r in httpx_mock.get_requests()
        )

    with TestClient(app) as client:
        client.post("/api/stream/start", json=body, headers=headers)
        time.sleep(0.3)  # 재생과 자연 종료 업로드가 끝나도록 기다림
        assert uploads() == 1
        assert client.post("/api/stream/stop", json=body, headers=headers).json()[
            "uploaded"
        ]
        assert uploads() == 2

        client.post("/api/stream/start", json=body, headers=headers)
        time.sleep(0.3)
        assert client.get("/health").json()["streaming"] is False
        stop = client.post("/api/stream/stop", json=body, headers=headers)
        assert stop.status_code == 404
        assert uploads() == 3
