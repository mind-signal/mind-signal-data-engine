"""동적 헤드셋 바인딩 재현/회귀 테스트.

문제 1: core/main.py가 HEADSET_ID_{idx} 환경변수로 헤드셋을 고정 바인딩한다.
원하는 동작: env를 무시하고 빈 문자열을 넘겨 SDK가 첫 연결 헤드셋을 자동 선택한다
(어떤 헤드셋이 붙어도 그 PC의 subject로 값 주입).

subject_index 1/2를 모두 파라미터화해 양쪽 HEADSET_ID env가 전부 무시되는지 잠근다.
"""

import sys

import pytest

import core.main as main_module


@pytest.mark.parametrize(
    ("subject_index", "env_var"),
    [("1", "HEADSET_ID_1"), ("2", "HEADSET_ID_2")],
)
def test_main_ignores_headset_id_env_and_lets_sdk_pick_first(
    monkeypatch, subject_index, env_var
):
    captured = {}

    class FakeStreamer:
        def __init__(
            self,
            group_id,
            subject_index,
            client_id,
            client_secret,
            headset_id="",
            **kwargs,
        ):
            captured["headset_id"] = headset_id

        def open(self):
            captured["opened"] = True

    monkeypatch.setattr(main_module, "MindSignalStreamer", FakeStreamer)
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    # 하드코딩 시나리오: 해당 subject의 헤드셋 ID가 env에 박혀 있어도
    monkeypatch.setenv(env_var, "INSIGHT2-HARDCODED")
    monkeypatch.setattr(sys, "argv", ["core.main", "group-xyz", subject_index])

    main_module.main()

    # 동적 바인딩: env의 하드코딩 ID를 절대 통과시키지 않고 빈 문자열을 넘겨야 한다.
    assert captured["headset_id"] == ""
