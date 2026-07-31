"""logbuffer 라인 버퍼링 테스트 — write 청크를 개행 기준 라인으로 적재하는지 검증함."""

import io

from server.services.logbuffer import _BUFFER, _Tee


def test_tee_splits_on_newlines_and_buffers_partial():
    """개행 기준 분리 + 부분 라인 잔여분 보관 확인함 (CodeRabbit)."""
    _BUFFER.clear()
    sink = io.StringIO()
    tee = _Tee(sink)

    tee.write("a\nb\n")
    assert list(_BUFFER)[-2:] == ["a", "b"]

    tee.write("c")  # 개행 없는 부분 라인 — 아직 미적재
    assert list(_BUFFER)[-1] == "b"

    tee.write("d\n")  # 이전 잔여분과 합쳐 "cd" 완성
    assert list(_BUFFER)[-1] == "cd"

    # 원본 스트림에는 전량 그대로 보존함
    assert sink.getvalue() == "a\nb\ncd\n"
