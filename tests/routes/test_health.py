"""GET /health 회귀 테스트.

대시보드가 각 DE의 subject_index를 표시할 수 있도록 /health가 이를 노출하는지 검증함.
값 바인딩이 끊기거나 상수화돼도 잡도록 settings 값을 sentinel로 패치해 검증함.
"""

from server.config import settings


def test_health_reports_subject_index(test_client, monkeypatch):
    monkeypatch.setattr(settings, "dual_2pc_subject_index", 2)
    response = test_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "mind-signal-data-engine"
    assert body["subject_index"] == 2


def test_health_reports_null_subject_index(test_client, monkeypatch):
    monkeypatch.setattr(settings, "dual_2pc_subject_index", None)
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["subject_index"] is None
