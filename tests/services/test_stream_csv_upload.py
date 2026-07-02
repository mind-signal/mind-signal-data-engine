"""stop_stream의 subject CSV 업로드 테스트 — Windows 하드킬로 on_close가 안 돌 때 대비."""

import os

import server.services.stream as stream_mod
import server.services.webhook as webhook_mod


def test_csv_dir_is_repo_parent_not_engine_root():
    """CSV 검색 루트는 _ENGINE_ROOT/csv가 아니라 그 부모/csv여야 함.

    core/streamer.py의 base_path(레포 부모/csv)에 실제 CSV가 저장되는데,
    stream.py는 server/services/ 아래라 동일한 parent×3이 레포 루트를 가리켜
    _ENGINE_ROOT/csv를 보면 원격 subject CSV를 못 찾고 업로드 skip됐음(경로 불일치 회귀).
    """
    assert stream_mod._CSV_DIR == os.path.join(
        os.path.dirname(stream_mod._ENGINE_ROOT), "csv"
    )
    assert stream_mod._CSV_DIR != os.path.join(stream_mod._ENGINE_ROOT, "csv")


def test_upload_subject_csv_uploads_latest(monkeypatch, tmp_path):
    """csv/ 에서 해당 group/subject의 최신 CSV를 골라 업로드함."""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    old = csv_dir / "subject_2_G_20260101_000000.csv"
    old.write_text("old")
    new = csv_dir / "subject_2_G_20260101_000001.csv"
    new.write_text("new")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))  # new가 더 최근

    monkeypatch.setattr(stream_mod, "_CSV_DIR", str(csv_dir))
    captured = {}
    monkeypatch.setattr(
        webhook_mod,
        "upload_csv_to_backend",
        lambda backend, path, secret: captured.update(path=path),
    )

    stream_mod._upload_subject_csv("G", 2)
    assert captured["path"] == str(new)


def test_upload_subject_csv_escapes_glob_metachars(monkeypatch, tmp_path):
    """group_id에 glob 메타문자([)가 있어도 리터럴 매칭함 (CodeRabbit)."""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    target = csv_dir / "subject_2_G[1]_20260101_000000.csv"
    target.write_text("x")

    monkeypatch.setattr(stream_mod, "_CSV_DIR", str(csv_dir))
    captured = {}
    monkeypatch.setattr(
        webhook_mod,
        "upload_csv_to_backend",
        lambda backend, path, secret: captured.update(path=path),
    )

    stream_mod._upload_subject_csv("G[1]", 2)
    assert captured.get("path") == str(target)


def test_upload_subject_csv_skip_when_none(monkeypatch, tmp_path):
    """해당 CSV가 없으면 업로드 skip함 (호출 0)."""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    monkeypatch.setattr(stream_mod, "_CSV_DIR", str(csv_dir))
    called = []
    monkeypatch.setattr(
        webhook_mod,
        "upload_csv_to_backend",
        lambda *a, **k: called.append(1),
    )

    stream_mod._upload_subject_csv("G", 2)
    assert called == []
