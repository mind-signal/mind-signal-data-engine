# 트러블슈팅 — Mind Signal Data Engine

## Cortex 연결 오류

- Emotiv App이 실행 중인지 확인
- `CLIENT_ID`, `CLIENT_SECRET`이 `.env.local`에 있는지 확인
- Emotiv 계정으로 App에 로그인되어 있는지 확인

## Redis 연결 오류

```bash
# Docker Redis가 실행 중인지 확인 (백엔드 폴더에서)
cd ../mind-signal-backend
docker-compose up -d
```

## 패키지 오류

```bash
conda activate mind-signal
pip install -r requirements.txt --break-system-packages
```

## Python 경로 (자주 실수함)

- conda 환경 Python 경로: `C:\Users\gs071\.conda\envs\mind-signal\python.exe`
- 시스템 Python이 아닌 반드시 위 경로를 사용함 (pytest, uvicorn 등 실행 시)
- conda 활성화 미선행 시 시스템 Python 3.13/3.9가 잡혀 의존성 깨짐
