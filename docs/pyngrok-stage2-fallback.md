# pyngrok Stage 2 fallback 보존 메모

이 문서는 `pyngrok` 의존성과 `registration_mode=ngrok` 분기를 제거하지 않는 이유를
코드 실측 근거와 함께 박제한 운영 메모다.

> 갱신 2026-06-28: rescue path 는 Phase 18.1 에서 Tailscale 로 확정됨(ngrok 은 R6 정합
> risk 로 권장 조합에서 제외, [[Tailscale Rescue Path Setup Guide]] 참조). 이에 따라
> `ngrok_auth_token` Settings 필드는 제거됨(아래 근거). `registration_mode` 와 `app.py`
> ngrok 분기, `pyngrok` 의존성은 비상 수동 경로 자산으로만 유지함.

---

## 코드 실측 근거

### server/config.py — registration_mode 와 ngrok_auth_token

`server/config.py` L11 에서 `registration_mode` 가 정의된다.

```python
registration_mode: Literal["local", "ngrok"] = "local"  # 등록 방식 선택
```

기본값은 `"local"` 이다. `"ngrok"` 을 선택하려면 환경변수 `REGISTRATION_MODE=ngrok` 을 설정한다.

`ngrok_auth_token` Settings 필드는 제거됨(2026-06-28). 근거: pydantic 은 환경변수를
`NGROK_AUTH_TOKEN`(언더스코어)으로 파싱하나 pyngrok 은 `NGROK_AUTHTOKEN`(언더스코어 없음)을
자동 탐지하므로, 필드에 값을 넣어도 pyngrok 에 전달되지 않아 dead weight 였음. `app.py` 의
`ngrok.connect()` 호출도 이 필드를 인자로 넘기지 않았음.

`registration_mode` 필드는 유지함. 비상 시 ngrok 을 수동 사용하려면 Settings 필드 없이
운영자가 os 환경에 pyngrok 표준 변수 `NGROK_AUTHTOKEN`(언더스코어 없음)을 직접 설정하면 됨.

### server/app.py — lifespan 내 ngrok 분기

`server/app.py` L60-65 의 `lifespan` 함수 내 public_url 결정 분기다.

```python
# public_url 결정 (registration_mode 기반)
if settings.registration_mode == "ngrok":
    from pyngrok import ngrok

    tunnel = ngrok.connect(settings.fastapi_port, bind_tls=True)
    public_url = tunnel.public_url
    print(f"ngrok 퍼블릭 URL 발급됨: {public_url}", flush=True)
else:  # local
    lan_ip = settings.lan_ip or socket.gethostbyname(socket.gethostname())
    public_url = f"http://{lan_ip}:{settings.fastapi_port}"
```

`lifespan` 종료(shutdown) 시에는 L209-212 에서 disconnect 한다.

```python
if settings.registration_mode == "ngrok":
    from pyngrok import ngrok

    ngrok.disconnect(public_url)
```

이 분기는 평소 dead path 이지만 escalation 자산이므로 삭제하지 않는다.

### requirements.txt — pyngrok 의존성

`requirements.txt` 에 `pyngrok==7.2.2` 가 포함되어 있다. 이 의존성을 제거하면
Stage 2 fallback 시 `from pyngrok import ngrok` 에서 ImportError 가 발생한다.
신규 환경 재현 시에도 `pip install -r requirements.txt` 로 함께 설치되므로 제거 금지.

---

## Stage 2 fallback 시나리오

Stage 1 default 는 `REGISTRATION_MODE=local`(또는 proxy forward) 이다.
ngrok 분기는 아래 상황에서만 fallback 경로로 활성화된다.

- Proxy/LAN 환경(RULE-1 ~ RULE-3)이 구성 불가한 경우
- 운영자 PC SPOF(단일 장애 지점) 발생으로 Proxy 가 응답하지 않는 경우
- 급박한 시연 환경에서 LAN 케이블 없이 인터넷만 사용할 수 있는 경우

fallback 절차:
1. DE 의 `.env.local` 에서 `REGISTRATION_MODE=ngrok` 으로 변경한다.
2. `NGROK_AUTHTOKEN`(pyngrok 표준 변수, 언더스코어 없음)을 os 환경에 실제 ngrok 계정 token 으로 설정한다. (구 `NGROK_AUTH_TOKEN` Settings 필드는 pyngrok 에 전달되지 않아 제거됨)
3. DE 를 재시작하면 lifespan 에서 ngrok 터널이 열리고 public_url 이 출력된다.
4. 이 public_url 을 BE 에 직접 등록하는 경로로 동작한다(`register_to_backend` 직통).

---

## 보안 주의사항

공개 ngrok URL 은 `/control/assign-group` 엔드포인트를 인터넷에 노출한다.
`ENGINE_SECRET_KEY` 가 placeholder 값이면 사실상 인증 없이 열리는 상태가 된다.

`server/app.py` 의 preflight soft-check(L50-57) 이 placeholder 감지 시 WARNING 을 출력한다.
실기기 테스트 후에는 ngrok 터널을 반드시 닫고(`ngrok.disconnect`), DE 를 재시작하여 local 모드로
복귀하거나 Proxy 모드로 전환할 것.

Stage 2 fallback 을 사용한 세션이 끝나면 `REGISTRATION_MODE` 를 `local` 로 원복하고
DE 를 재시작한다.
