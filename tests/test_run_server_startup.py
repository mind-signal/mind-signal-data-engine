"""run_server 기동 정책 테스트 — DEV_RELOAD 파싱과 stdout 인코딩 강제 검증함 (OPS-W006)"""

import run_server


def test_dev_reload_defaults_to_true(monkeypatch):
    """미설정이면 개발자 기본인 reload 유지함."""
    monkeypatch.delenv("DEV_RELOAD", raising=False)
    assert run_server._dev_reload() is True


def test_dev_reload_zero_disables(monkeypatch):
    """런처 경로는 0을 주입해 단일 프로세스로 기동함."""
    monkeypatch.setenv("DEV_RELOAD", "0")
    assert run_server._dev_reload() is False


def test_dev_reload_one_enables(monkeypatch):
    monkeypatch.setenv("DEV_RELOAD", "1")
    assert run_server._dev_reload() is True


def test_io_env_defaults_are_injected(monkeypatch):
    """미설정 환경에서 자식 프로세스용 기본값 둘 다 주입함."""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    run_server._apply_io_env()
    assert run_server.os.environ["PYTHONIOENCODING"] == "utf-8"
    assert run_server.os.environ["PYTHONUNBUFFERED"] == "1"


def test_explicit_io_env_is_preserved(monkeypatch):
    """사용자 명시 값은 덮어쓰지 않음 — setdefault 의미 고정함."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp949")
    run_server._apply_io_env()
    assert run_server.os.environ["PYTHONIOENCODING"] == "cp949"


def test_force_utf8_stdout_is_safe_without_reconfigure(monkeypatch):
    """reconfigure 보유 스트림엔 utf-8 지정, 미보유 스트림은 예외 없이 통과함."""
    calls = []

    class _Reconfigurable:
        def reconfigure(self, encoding):
            calls.append(encoding)

    class _Plain:
        pass

    monkeypatch.setattr(run_server.sys, "stdout", _Reconfigurable())
    monkeypatch.setattr(run_server.sys, "stderr", _Plain())
    run_server._force_utf8_stdout()
    assert calls == ["utf-8"]
