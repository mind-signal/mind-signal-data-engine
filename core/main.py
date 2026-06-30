import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from core.streamer import MindSignalStreamer

# 환경 변수 로드 수행함
load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")


def main():
    """
    [Main] 명령어 인자를 통해 그룹 및 피실험자 정보를 주입받아 엔진 구동함
    """
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")

    # 인자 예외 처리 및 안내 문구 출력함
    if len(sys.argv) < 3:
        print("인자가 부족함. 사용법: python -m core.main <groupId> <subjectIndex>")
        sys.exit(1)

    if not client_id or not client_secret:
        print("환경 변수 파일에서 API 키를 확인해야 함.")
        sys.exit(1)

    group_id = sys.argv[1]
    subject_index = sys.argv[2]

    # 동적 바인딩: 헤드셋 ID를 고정하지 않음. 빈 문자열을 넘기면 SDK가 이 PC에
    # 연결된 첫 헤드셋을 자동 선택해 subject_index 스트림으로 값을 주입함.
    # 헤드셋 ID 하드코딩 제거함. 어떤 헤드셋이 붙어도 동작함 (PC당 헤드셋 1대 전제).
    headset_id = ""

    # proxy 연동 설정 로드함 (ALIGNMENT_LOCATION=proxy 시 proxy 모드 활성화)
    alignment_location = os.getenv("ALIGNMENT_LOCATION", "be")
    proxy_url = os.getenv("PROXY_URL")
    engine_secret_key = os.getenv("ENGINE_SECRET_KEY", "")
    max_proxy_post_retries_raw = os.getenv("MAX_PROXY_POST_RETRIES", "2")
    max_proxy_post_retries = int(max_proxy_post_retries_raw)
    proxy_health_poll_interval_sec = (
        int(os.getenv("PROXY_HEALTH_POLL_INTERVAL_MS", "1000")) / 1000.0
    )
    proxy_fail_closed_threshold_ms = int(os.getenv("FAIL_CLOSED_THRESHOLD_MS", "3000"))

    print(f"Mind Signal Engine 구동 시작함 (Group: {group_id}, Index: {subject_index})")
    print(
        f"[INFO] 헤드셋 동적 선택: Cortex가 보고한 첫 헤드셋을 subject {subject_index}로 사용함"
    )

    # 스트리머 인스턴스 생성 및 실행 수행함
    streamer = MindSignalStreamer(
        group_id,
        subject_index,
        client_id,
        client_secret,
        headset_id=headset_id,
        alignment_location=alignment_location,
        proxy_url=proxy_url,
        engine_secret_key=engine_secret_key,
        max_proxy_post_retries=max_proxy_post_retries,
        proxy_health_poll_interval_sec=proxy_health_poll_interval_sec,
        proxy_fail_closed_threshold_ms=proxy_fail_closed_threshold_ms,
    )
    streamer.open()


if __name__ == "__main__":
    main()
