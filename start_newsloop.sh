#!/bin/zsh
set -euo pipefail

SCRIPT_PATH="$0"
while [ -L "$SCRIPT_PATH" ]; do
  LINK_TARGET="$(readlink "$SCRIPT_PATH")"
  case "$LINK_TARGET" in
    /*) SCRIPT_PATH="$LINK_TARGET" ;;
    *) SCRIPT_PATH="$(dirname "$SCRIPT_PATH")/$LINK_TARGET" ;;
  esac
done
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"

cd "$SCRIPT_DIR" || exit 1

PID_DIR="/tmp/newsloop_pids"
API_PID_FILE="$PID_DIR/backend.pid"
DAEMON_PID_FILE="$PID_DIR/daemon.pid"
STREAMLIT_PID_FILE="$PID_DIR/streamlit.pid"
API_LOG="/tmp/newsloop_backend.log"
DAEMON_LOG="/tmp/newsloop_daemon.log"
STREAMLIT_LOG="/tmp/newsloop_streamlit.log"
BACKUP_LOG="/tmp/newsloop_backup.log"
VENV_DIR=""
PYTHON_BIN=""

mkdir -p "$PID_DIR"

if ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl not found in PATH" >&2
  exit 1
fi

process_running() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

stop_pidfile() {
  local pid_file="$1"
  [ -f "$pid_file" ] || return 0
  local pid
  pid=$(cat "$pid_file" 2>/dev/null || true)
  if process_running "$pid"; then
    kill -15 "$pid" >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
      if ! process_running "$pid"; then
        break
      fi
      sleep 0.2
    done
    if process_running "$pid"; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  fi
  rm -f "$pid_file"
}

graceful_stop_port() {
  local port="$1"
  local pattern="$2"
  local pids matched cmd pid
  pids=$(lsof -ti:"$port" 2>/dev/null || true)
  [ -z "$pids" ] && return 0
  matched=""
  if [ -n "$pattern" ]; then
    for pid in $pids; do
      cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
      if echo "$cmd" | grep -q "$pattern"; then
        matched="$matched $pid"
      fi
    done
    pids="$matched"
    [ -z "$pids" ] && return 0
  fi
  echo "$pids" | xargs kill -15 >/dev/null 2>&1 || true
  for _ in $(seq 1 15); do
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [ -n "$pattern" ]; then
      matched=""
      for pid in $pids; do
        cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
        if echo "$cmd" | grep -q "$pattern"; then
          matched="$matched $pid"
        fi
      done
      pids="$matched"
    fi
    [ -z "$pids" ] && return 0
    sleep 0.2
  done
  echo "$pids" | xargs kill -9 >/dev/null 2>&1 || true
}

graceful_stop_pattern() {
  local pattern="$1"
  pkill -15 -f "$pattern" >/dev/null 2>&1 || true
  for _ in $(seq 1 15); do
    if ! pgrep -f "$pattern" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  pkill -9 -f "$pattern" >/dev/null 2>&1 || true
}

# 先停止舊程序，避免端口衝突
stop_pidfile "$API_PID_FILE"
stop_pidfile "$DAEMON_PID_FILE"
stop_pidfile "$STREAMLIT_PID_FILE"
graceful_stop_port 8000 "backend/app.py"
graceful_stop_port 8501 "streamlit/hybrid_dashboard.py"
graceful_stop_pattern "auto_grader_daemon.py"
graceful_stop_pattern "streamlit run streamlit/hybrid_dashboard.py --server.port 8501 --server.address 127.0.0.1"

if [ -x "./venv/bin/python3" ]; then
  VENV_DIR="./venv"
elif [ -x "./.venv/bin/python3" ]; then
  VENV_DIR="./.venv"
else
  echo "Error: venv not found at ./venv or ./.venv" >&2
  exit 1
fi

PYTHON_BIN="$VENV_DIR/bin/python3"
export PATH="$VENV_DIR/bin:$PATH"

export YF_USE_CURL=0

# 啟動 backend（背景模式，避免 shell 關閉時整體被殺）
nohup "$PYTHON_BIN" backend/app.py > "$API_LOG" 2>&1 &
API_PID=$!
echo "$API_PID" > "$API_PID_FILE"

BACKEND_OK=false
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    BACKEND_OK=true
    break
  fi
  sleep 1
done
if [ "$BACKEND_OK" != "true" ]; then
  echo "後端啟動失敗，請查看 $API_LOG" >&2
  tail -n 80 "$API_LOG" 2>/dev/null || true
  exit 1
fi

# 啟動 daemon（背景模式）
nohup "$PYTHON_BIN" auto_grader_daemon.py > "$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!
echo "$DAEMON_PID" > "$DAEMON_PID_FILE"

# 啟動 streamlit（背景模式）
nohup "$PYTHON_BIN" -m streamlit run streamlit/hybrid_dashboard.py --server.port 8501 --server.address 127.0.0.1 > "$STREAMLIT_LOG" 2>&1 &
STREAMLIT_PID=$!
echo "$STREAMLIT_PID" > "$STREAMLIT_PID_FILE"

STREAMLIT_OK=false
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8501/_stcore/health >/dev/null 2>&1; then
    STREAMLIT_OK=true
    break
  fi
  sleep 1
done
if [ "$STREAMLIT_OK" != "true" ]; then
  echo "儀表板啟動失敗，請查看 $STREAMLIT_LOG" >&2
  tail -n 80 "$STREAMLIT_LOG" 2>/dev/null || true
  exit 1
fi

echo "NewsLoop 已啟動："
echo "  - Backend   : http://localhost:8000 (PID $API_PID)"
echo "  - Dashboard : http://localhost:8501 (PID $STREAMLIT_PID)"
echo "  - Daemon    : PID $DAEMON_PID"
echo "日誌："
echo "  - $API_LOG"
echo "  - $DAEMON_LOG"
echo "  - $STREAMLIT_LOG"
echo "  - $BACKUP_LOG"

# 輕量本地備份：只有當備份過舊時才執行，不阻斷啟動流程
if [ "${NEWSLOOP_AUTO_BACKUP_ON_START:-1}" = "1" ]; then
  BACKUP_STALE_HOURS="${NEWSLOOP_BACKUP_STALE_HOURS:-24}"
  if ! "$PYTHON_BIN" scripts/runtime_recovery.py maybe-backup --stale-hours "$BACKUP_STALE_HOURS" > "$BACKUP_LOG" 2>&1; then
    echo "警告：輕量 backup 未成功，請查看 $BACKUP_LOG" >&2
  fi
fi

# 可選：自動打開瀏覽器
if [ "${NEWSLOOP_NO_OPEN:-0}" != "1" ] && command -v open >/dev/null 2>&1; then
  open "http://localhost:8501" >/dev/null 2>&1 || true
fi
