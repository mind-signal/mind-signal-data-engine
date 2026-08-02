"""런타임 로그가 cp949 콘솔에서 죽지 않는지 검증함 (EEG-W004)

stdout이 tty가 아니면 Python이 인코딩을 ANSI 코드페이지(Windows 한국어는 cp949)로
확정함. `server/services/stream.py`가 `core.main` stdout을 로그 파일로 redirect하므로
그 경로는 상시 cp949이고, em dash 같은 문자를 출력하면 UnicodeEncodeError로 프로세스가
죽음. 2026-06-30 측정 1건이 실제로 이 원인으로 0초 종료함.

`run_server.py` 진입점이 utf-8을 강제하지만 그것을 거치지 않는 진입점이 생기면 방어가
빠지므로 소스 자체도 안전하게 유지함.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# sdk/는 Emotiv 제공 원본이라 수정 금지(AGENTS.md 5장). cortex.py에 U+274C가 남아
# 있으나 손댈 수 없어 진입점 방어에 의존하므로 스캔 대상에서 제외함.
SCANNED_DIRS = ("core", "server")


def _print_literals(path: Path):
    """파일 안 print() 호출 인자의 문자열 리터럴을 (줄번호, 값)으로 반환함.

    docstring과 주석은 출력되지 않아 무해하므로 제외함 — 포함하면 오탐이 수십 건임.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        is_print = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        if not (is_print and node.func.id == "print"):
            continue
        for arg in ast.walk(node):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield node.lineno, arg.value


def test_print_literals_survive_cp949_stdout():
    """print로 나가는 문자열은 전부 cp949로 인코딩 가능해야 함."""
    offenders = []
    for directory in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            for lineno, value in _print_literals(path):
                try:
                    value.encode("cp949")
                except UnicodeEncodeError as exc:
                    char = value[exc.start : exc.end]
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{lineno} {char!r} (U+{ord(char):04X})")

    assert (
        not offenders
    ), "cp949 콘솔에서 UnicodeEncodeError로 죽는 로그:\n" + "\n".join(offenders)
