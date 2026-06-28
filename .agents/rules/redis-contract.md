
# Redis 채널 계약 — Mind Signal Data Engine

## 채널 키 Convention

```
mind-signal:{groupId}:subject:{subjectIndex}
```

- `groupId`: MongoDB ObjectId 문자열 (백엔드가 spawn 시 `sys.argv[1]`로 전달)
- `subjectIndex`: 0-based 정수 (`sys.argv[2]`)
- **고정 채널명 사용 금지** — `mind-signal-live` 같은 글로벌 채널 재도입 금지
- **PC/host 정보 포함 금지** — 채널 키에 IP/hostname 삽입 금지

## 발행 메시지 포맷

```json
{
  "type": "brain_sync_all",
  "groupId": "<string>",
  "subjectIndex": "<int>",
  "waves": {
    "delta": 0.0,
    "theta": 0.0,
    "alpha": 0.0,
    "beta": 0.0,
    "gamma": 0.0
  },
  "metrics": {
    "focus": 0.0,
    "engagement": 0.0,
    "interest": 0.0,
    "excitement": 0.0,
    "stress": 0.0,
    "relaxation": 0.0
  },
  "time": "2025-01-01 12:00:00.000000"
}
```

> 출처: `core/streamer.py` 193~201줄의 payload 구조와 정확히 일치.

## 진입점 변경 이력

`feat/session-group-pairing` (`73f5e36`)에서 다음과 같이 변경됨:

| 항목 | 변경 전 | 변경 후 |
|------|--------|--------|
| 진입점 | `core.streamer` 상시 실행 (start-dev.bat) | `core.main` 세션별 spawn (백엔드가 호출) |
| 채널 | `mind-signal-live` (고정) | `mind-signal:{groupId}:subject:{subjectIndex}` (동적) |
| 인수 | 없음 | `sys.argv[1]=groupId`, `sys.argv[2]=subjectIndex` |

**이유**: 동시 다중 세션 지원 — 상시 실행 방식은 단일 Python 프로세스가 모든 세션을 처리해야 하므로 동시 측정 불가. `core.streamer`는 `if __name__ == "__main__"` 블록이 없어 모듈로 실행 시 즉시 종료됨 — 직접 실행 진입점으로 사용 불가, 반드시 `core.main` 경유.
