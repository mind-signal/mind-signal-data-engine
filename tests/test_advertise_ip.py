"""DE advertise IP 탐지 테스트 — LAN_IP 미지정 시 Tailscale 대역 자동 선택 검증함."""

from server import app as app_mod


def test_detect_tailscale_ip_picks_cgnat(monkeypatch):
    """여러 어댑터 중 100.64.0.0/10 대역(Tailscale)을 선택함."""
    monkeypatch.setattr(app_mod.socket, "gethostname", lambda: "nb")
    monkeypatch.setattr(
        app_mod.socket,
        "gethostbyname_ex",
        lambda h: ("nb", [], ["172.17.0.1", "100.86.237.53", "192.168.0.5"]),
    )
    assert app_mod._detect_tailscale_ip() == "100.86.237.53"


def test_detect_tailscale_ip_none_when_absent(monkeypatch):
    """Tailscale 대역 IP가 없으면 None 반환함 (기존 폴백으로 위임)."""
    monkeypatch.setattr(app_mod.socket, "gethostname", lambda: "nb")
    monkeypatch.setattr(
        app_mod.socket,
        "gethostbyname_ex",
        lambda h: ("nb", [], ["172.17.0.1", "192.168.0.5"]),
    )
    assert app_mod._detect_tailscale_ip() is None
