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


def test_resolve_honors_explicit_tailscale_ip(monkeypatch):
    """명시 LAN_IP가 Tailscale 대역이면 그대로 사용함."""
    monkeypatch.setattr(app_mod, "_detect_tailscale_ip", lambda: "100.99.99.99")
    assert app_mod._resolve_advertise_ip("100.86.237.53") == "100.86.237.53"


def test_resolve_ignores_stale_non_tailscale_ip(monkeypatch):
    """명시 LAN_IP가 대역 밖(스테일 LAN/Wi-Fi)이면 무시하고 자동탐지로 대체함 (CodeRabbit)."""
    monkeypatch.setattr(app_mod, "_detect_tailscale_ip", lambda: "100.86.237.53")
    # 노트북 B가 등록하던 LAN IP — 이제 무시되고 Tailscale IP로 self-heal됨
    assert app_mod._resolve_advertise_ip("10.26.140.41") == "100.86.237.53"


def test_resolve_none_uses_autodetect(monkeypatch):
    """LAN_IP 미지정 시 자동탐지 결과 사용함."""
    monkeypatch.setattr(app_mod, "_detect_tailscale_ip", lambda: "100.86.237.53")
    assert app_mod._resolve_advertise_ip(None) == "100.86.237.53"
