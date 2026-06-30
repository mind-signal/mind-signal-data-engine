"""GET /health 회귀 테스트.

대시보드가 각 DE의 subject_index를 표시할 수 있도록 /health가 이를 노출하는지 검증함.
"""


def test_health_reports_subject_index(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "mind-signal-data-engine"
    assert "subject_index" in body
