"""DE 서버 로그 인메모리 링버퍼 — 대시보드 로그 조회용임 (원격 노트북 B 진단)."""

import sys
from collections import deque
from typing import IO

# 최근 서버 로그 라인 보관 링버퍼 — 최대 1000줄 유지함
_BUFFER: deque[str] = deque(maxlen=1000)


class _Tee:
    """원본 스트림에 그대로 쓰면서 링버퍼에 개행 기준 라인 캡처하는 래퍼임."""

    def __init__(self, original: IO[str]) -> None:
        self._original = original
        # 개행 없이 끊긴 부분 라인 잔여분 — 다음 write까지 보관함 (tail N 정확도)
        self._remainder = ""

    def write(self, data: str) -> int:
        # 원본 콘솔 출력 보존함
        n = self._original.write(data)
        combined = self._remainder + data
        parts = combined.split("\n")
        # 마지막 조각은 개행으로 끝나지 않은 부분 라인 — 잔여분으로 보관함
        self._remainder = parts.pop()
        for line in parts:
            line = line.rstrip("\r")
            if line.strip():
                _BUFFER.append(line)
        return n

    def flush(self) -> None:
        self._original.flush()

    def __getattr__(self, name: str):
        # isatty 등 기타 속성은 원본으로 위임함
        return getattr(self._original, name)


def install() -> None:
    """sys.stdout/stderr에 tee 설치해 서버 print 로그를 링버퍼에 캡처함 (멱등)."""
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr)


def tail(n: int = 200) -> list[str]:
    """최근 n개 로그 라인 반환함 (n<=0 이면 전체)."""
    lines = list(_BUFFER)
    if n <= 0:
        return lines
    return lines[-n:]
