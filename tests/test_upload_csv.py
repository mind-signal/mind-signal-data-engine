"""upload_csv_to_backend 단위 테스트 (2-PC CSV 집계)

측정 종료 시 노트북 B DE가 자기 subject CSV를 operator BE로 업로드함.
soft-fail: 업로드 실패해도 측정 종료 흐름을 깨지 않아야 함.
"""

from pytest_httpx import HTTPXMock

from server.services.webhook import upload_csv_to_backend

MOCK_BACKEND_URL = "http://mock-backend:5000"
SECRET = "test-secret"
FILENAME = "subject_2_6a413cef58664859f44ee519_20260629_002612.csv"


def test_upload_csv_success(httpx_mock: HTTPXMock, tmp_path) -> None:
    """200 응답 시 CSV 본문+secret 헤더+filename 쿼리로 POST 호출 + True 반환함"""
    csv = tmp_path / FILENAME
    csv.write_text("time,alpha\n2026-06-29 00:26:12,0.3\n", encoding="utf-8")

    httpx_mock.add_response(method="POST", status_code=200, json={"status": "success"})

    ok = upload_csv_to_backend(MOCK_BACKEND_URL, str(csv), SECRET)

    assert ok is True
    req = httpx_mock.get_request()
    assert req is not None
    assert req.method == "POST"
    assert "/api/engine/csv-upload" in str(req.url)
    assert f"filename={FILENAME}" in str(req.url)
    assert req.headers["X-Engine-Secret"] == SECRET
    assert b"alpha" in req.content


def test_upload_csv_soft_fail_on_5xx(httpx_mock: HTTPXMock, tmp_path) -> None:
    """5xx 응답이어도 raise 안 하고 False 반환함 (측정 종료 흐름 보호)"""
    csv = tmp_path / FILENAME
    csv.write_text("x\n", encoding="utf-8")

    httpx_mock.add_response(method="POST", status_code=500)

    ok = upload_csv_to_backend(MOCK_BACKEND_URL, str(csv), SECRET)

    assert ok is False


def test_upload_csv_missing_file_soft_fail(tmp_path) -> None:
    """파일이 없으면 raise 안 하고 False 반환함"""
    ok = upload_csv_to_backend(MOCK_BACKEND_URL, str(tmp_path / "nope.csv"), SECRET)
    assert ok is False
