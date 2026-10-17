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


def test_upload_csv_non_utf8_bytes_does_not_raise(
    httpx_mock: HTTPXMock, tmp_path
) -> None:
    """UTF-8로 디코딩 불가한 CSV여도 raise 안 하고 바이트 그대로 전송함.

    회귀 재현 — 왕복 인코딩(텍스트로 읽고 다시 encode) 구현에서는
    UnicodeDecodeError가 발생하고, 그것은 ValueError 계열이라
    soft-fail의 except (httpx.HTTPError, OSError)를 뚫고 측정 종료 흐름을 깬다.
    """
    csv = tmp_path / FILENAME
    # cp949로 인코딩된 한글 — UTF-8 디코딩 시 UnicodeDecodeError 발생함
    csv.write_bytes("time,alpha\n측정,0.3\n".encode("cp949"))

    httpx_mock.add_response(method="POST", status_code=200, json={"status": "success"})

    ok = upload_csv_to_backend(MOCK_BACKEND_URL, str(csv), SECRET)

    assert ok is True
    req = httpx_mock.get_request()
    assert req is not None
    # 바이트가 변형 없이 그대로 실려야 함
    assert req.content == csv.read_bytes()
    assert req.headers["Content-Type"] == "text/csv"
