#!/usr/bin/env bash
# OPS-W019 맥미니 한 대 재생 하네스 기동, 종료, 정리 스크립트임.
#
#   harness.sh start --bus memory|redis --csv1 {subject1 CSV} --csv2 {subject2 CSV}
#                    [--relay-delay-subject 1|2 --relay-delay-ms N]
#   harness.sh stop
#   harness.sh clean {groupId}
#
# 기동 순서: 백엔드(5100) → [중계기(5101)] → 프록시(5050) → 분석 엔진(5002)
# → 가짜 엔진(5011, 5012). 중계기 인자를 주면 프록시 BACKEND_URL을 중계기로 돌려
# 그 subject의 proxy:sample만 백엔드에 늦게 도착시킴 (relay.js, EEG-W006 재현).
# MongoDB(27017)와 Redis(6379)는 이미 떠 있어야 함 (ms-replay-mongo, ms-replay-redis).
# JWT와 엔진 시크릿은 실행마다 무작위로 만들고 프로세스 환경으로만 넘김(파일에 안 남김).
# 각 저장소의 .env 파일은 읽히지 않게 함: 백엔드는 NODE_ENV=replay(.env.replay 없음),
# 프록시는 DOTENV_CONFIG_PATH=/dev/null. 분석 엔진은 이 스크립트가 든 엔진 저장소에서
# 돌므로 워크트리(.env.local 없음)에서 실행함. 원본 clone에서 돌리면 .env.local을 읽음.
# 경로 변수는 env.example 참조.
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
MS_ROOT="${MS_ROOT:-$HOME/Team-project/mind-signal}"
BACKEND_DIR="${BACKEND_DIR:-$MS_ROOT/mind-signal-backend}"
PROXY_DIR="${PROXY_DIR:-$MS_ROOT/mind-signal-proxy}"
CSV_DIR="${CSV_DIR:-$MS_ROOT/csv}"
PYTHON="${PYTHON:-$MS_ROOT/mind-signal-data-engine/.venv/bin/python}"
RUN_DIR="${RUN_DIR:-$MS_ROOT/.replay-run}"
MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017/mindsignal-replay}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379}"

BE_PORT=5100
PX_PORT=5050
AE_PORT=5002
FE1_PORT=5011
FE2_PORT=5012
RELAY_PORT=5101
APP_PORTS=("$BE_PORT" "$PX_PORT" "$AE_PORT" "$FE1_PORT" "$FE2_PORT")

die() { echo "[harness] $*" >&2; exit 1; }
listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

wait_port() {
  local port=$1 name=$2
  for _ in $(seq 1 90); do
    listening "$port" && { echo "[harness] $name :$port 준비됨"; return 0; }
    sleep 1
  done
  die "$name :$port 90초 안에 안 뜸. 로그: $RUN_DIR/$name.log"
}

# 각 프로세스를 자기 프로세스 그룹으로 띄워 stop에서 자식(npm→node)까지 함께 끝냄
launch() {
  local name=$1 dir=$2
  shift 2
  (cd "$dir" && exec "$@") >"$RUN_DIR/$name.log" 2>&1 &
  echo "$! $name" >>"$RUN_DIR/pids"
}

cmd_start() {
  local bus="" csv1="" csv2="" relay_subject="" relay_ms=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --bus) bus=$2; shift 2 ;;
      --csv1) csv1=$2; shift 2 ;;
      --csv2) csv2=$2; shift 2 ;;
      --relay-delay-subject) relay_subject=$2; shift 2 ;;
      --relay-delay-ms) relay_ms=$2; shift 2 ;;
      *) die "알 수 없는 인자: $1" ;;
    esac
  done
  [ "$bus" = memory ] || [ "$bus" = redis ] || die "--bus 는 memory 또는 redis"
  [ -f "$csv1" ] && [ -f "$csv2" ] || die "--csv1, --csv2 파일이 필요함"
  # launch가 ENGINE_DIR로 이동하므로 상대 경로를 절대 경로로 바꿈 (bash 3.2에 realpath 없음)
  csv1="$(cd "$(dirname "$csv1")" && pwd)/$(basename "$csv1")"
  csv2="$(cd "$(dirname "$csv2")" && pwd)/$(basename "$csv2")"
  [ -e "$RUN_DIR/pids" ] && die "이전 실행이 남아 있음. 먼저 harness.sh stop"
  if [ -n "$relay_subject$relay_ms" ]; then
    [[ "$relay_subject" =~ ^[12]$ ]] && [[ "$relay_ms" =~ ^[0-9]+$ ]] ||
      die "--relay-delay-subject 1|2 와 --relay-delay-ms N 을 함께 줘야 함"
    APP_PORTS+=("$RELAY_PORT")
  fi

  local busy=()
  for p in "${APP_PORTS[@]}"; do listening "$p" && busy+=("$p"); done
  [ ${#busy[@]} -eq 0 ] || die "포트 사용 중: ${busy[*]}. 아무것도 종료하지 않고 거부함"
  listening 27017 || die "MongoDB 27017 미기동 (docker start ms-replay-mongo)"
  listening 6379 || die "Redis 6379 미기동 (docker start ms-replay-redis)"

  mkdir -p "$RUN_DIR" "$CSV_DIR"
  set -m
  local secret jwt
  secret=$(openssl rand -hex 32)
  jwt=$(openssl rand -hex 32)

  launch backend "$BACKEND_DIR" env NODE_ENV=replay PORT="$BE_PORT" \
    MONGODB_URI="$MONGODB_URI" JWT_SECRET_KEY="$jwt" JWT_EXPIRES_IN=2h \
    MESSAGE_BUS="$bus" REDIS_URL="$REDIS_URL" ENGINE_SECRET_KEY="$secret" \
    DATA_ENGINE_URL="http://127.0.0.1:$AE_PORT" CSV_STORAGE_DIR="$CSV_DIR" \
    npm run dev
  wait_port "$BE_PORT" backend

  # 프록시가 보는 백엔드 주소임. 중계기를 쓰면 중계기가 HTTP와 /proxy를 백엔드로 넘김
  local proxy_backend="http://127.0.0.1:$BE_PORT"
  if [ -n "$relay_subject" ]; then
    launch relay "$ENGINE_DIR" env NODE_PATH="$BACKEND_DIR/node_modules" \
      node scripts/replay/relay.js --port "$RELAY_PORT" --backend-url "$proxy_backend" \
      --delay-subject "$relay_subject" --delay-ms "$relay_ms"
    wait_port "$RELAY_PORT" relay
    proxy_backend="http://127.0.0.1:$RELAY_PORT"
  fi

  launch proxy "$PROXY_DIR" env DOTENV_CONFIG_PATH=/dev/null PROXY_PORT="$PX_PORT" \
    BACKEND_URL="$proxy_backend" ENGINE_SECRET_KEY="$secret" \
    npm run dev
  wait_port "$PX_PORT" proxy

  # 분석 엔진은 CSV 폴더가 코드 상수라 실행 전에 CSV_DIR로 바꿔 끼움
  launch analysis "$ENGINE_DIR" env FASTAPI_PORT="$AE_PORT" REGISTRATION_MODE=local \
    LAN_IP=127.0.0.1 BACKEND_URL="http://127.0.0.1:$BE_PORT" \
    ENGINE_SECRET_KEY="$secret" MESSAGE_BUS=redis ALIGNMENT_LOCATION=be \
    CSV_DIR="$CSV_DIR" "$PYTHON" -c '
import os, pathlib, uvicorn
import server.services.analysis as analysis
analysis.CSV_BASE_DIR = pathlib.Path(os.environ["CSV_DIR"])
from server.app import app
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["FASTAPI_PORT"]))
'
  wait_port "$AE_PORT" analysis

  local i port csv extra
  for i in 1 2; do
    # REPLAY1_ARGS, REPLAY2_ARGS 로 엔진별 지연·재전송 옵션을 줌 (예: "--delay-ms 150")
    extra="REPLAY${i}_ARGS"
    port=$([ "$i" = 1 ] && echo "$FE1_PORT" || echo "$FE2_PORT")
    csv=$([ "$i" = 1 ] && echo "$csv1" || echo "$csv2")
    launch "replay$i" "$ENGINE_DIR" env ENGINE_SECRET_KEY="$secret" \
      "$PYTHON" scripts/replay_data_engine.py --subject-index "$i" --port "$port" \
      --csv "$csv" --csv-dir "$CSV_DIR" \
      --proxy-url "http://127.0.0.1:$PX_PORT" --backend-url "http://127.0.0.1:$BE_PORT" \
      ${!extra:-}
    wait_port "$port" "replay$i"
  done
  echo "[harness] 기동 완료 (MESSAGE_BUS=$bus). 로그: $RUN_DIR"
  echo "[harness] 드라이버: $PYTHON $ENGINE_DIR/scripts/replay_driver.py --csv-dir $CSV_DIR"
}

cmd_stop() {
  [ -f "$RUN_DIR/pids" ] || { echo "[harness] 실행 기록 없음"; return 0; }
  local pid name
  while read -r pid name; do
    # 이 스크립트가 띄운 프로세스 그룹만 종료함
    kill -TERM -- "-$pid" 2>/dev/null && echo "[harness] $name($pid) 종료 요청"
  done <"$RUN_DIR/pids"
  sleep 3
  while read -r pid _; do kill -KILL -- "-$pid" 2>/dev/null || true; done <"$RUN_DIR/pids"
  # 중계기를 띄운 실행이면 그 포트도 닫혔는지 봄
  grep -q ' relay$' "$RUN_DIR/pids" && APP_PORTS+=("$RELAY_PORT")
  rm "$RUN_DIR/pids"
  local left=()
  for p in "${APP_PORTS[@]}"; do listening "$p" && left+=("$p"); done
  [ ${#left[@]} -eq 0 ] || die "아직 열린 포트: ${left[*]}"
  echo "[harness] 종료 완료"
}

cmd_clean() {
  local gid=${1:-}
  [[ "$gid" =~ ^[0-9a-f]{24}$ ]] || die "clean 인자는 24자리 hex groupId"
  # 재생 실행이 만든 이번 groupId의 CSV만 지움
  find "$CSV_DIR" -maxdepth 1 -type f -name "subject_[12]_${gid}_*.csv" -print -delete
}

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  -h|--help) usage ;;
  start) shift; cmd_start "$@" ;;
  stop) cmd_stop ;;
  clean) shift; cmd_clean "$@" ;;
  *) usage >&2; exit 1 ;;
esac
