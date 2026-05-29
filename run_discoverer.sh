#!/usr/bin/env bash
# run_discoverer.sh — обёртка для Discoverer v4:
#   1. Останавливает парсер
#   2. Запускает Discoverer v4
#   3. Стартует парсер обратно

set -e

LEADBOT_DIR="${LEADBOT_DIR:-/root/leadbot}"
PYTHON="$LEADBOT_DIR/venv/bin/python"
PARSER_LOG="$LEADBOT_DIR/logs/indexer.log"
PARSER_PID_FILE="$LEADBOT_DIR/.pid_indexer"

[ -d "$LEADBOT_DIR" ] || { echo "No $LEADBOT_DIR"; exit 1; }
[ -x "$PYTHON" ]    || { echo "No venv: $PYTHON"; exit 1; }
[ -f "$LEADBOT_DIR/src/discoverer_v4.py" ] || { echo "No src/discoverer_v4.py"; exit 1; }

ts() { date "+%Y-%m-%d %H:%M:%S"; }

echo "========================================================"
echo "  Discoverer v4 — wrapper with parser restart"
echo "========================================================"
echo

# 1. Stop parser
echo "[$(ts)] Stopping parser..."
PARSER_PID=$(pgrep -f "parser_apex_ai.py" | head -1 || true)
if [ -n "$PARSER_PID" ]; then
    kill -TERM "$PARSER_PID" 2>/dev/null || true
    sleep 5
    if kill -0 "$PARSER_PID" 2>/dev/null; then
        kill -9 "$PARSER_PID" 2>/dev/null || true
        sleep 2
    fi
    echo "    Parser stopped"
else
    echo "    Parser not running"
fi

sleep 3

# 2. Run Discoverer v4
echo
echo "[$(ts)] Running Discoverer v4..."
echo "--------------------------------------------------------"
cd "$LEADBOT_DIR/src"
"$PYTHON" discoverer_v4.py
DISC_EXIT=$?
echo "--------------------------------------------------------"
if [ $DISC_EXIT -ne 0 ]; then
    echo "[$(ts)] Discoverer exited with code $DISC_EXIT"
fi

# 3. Restart parser
echo
echo "[$(ts)] Restarting parser..."
cd "$LEADBOT_DIR/src"
nohup "$PYTHON" parser_apex_ai.py > "$PARSER_LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PARSER_PID_FILE"
sleep 4

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "    Parser running (PID=$NEW_PID)"
else
    echo "    Parser failed to start! See $PARSER_LOG"
    tail -20 "$PARSER_LOG" 2>/dev/null || true
    exit 1
fi

echo
echo "Done."
