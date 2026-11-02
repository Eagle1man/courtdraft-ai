#!/bin/bash
# Остановить backend по порту и выгрузить модель.
set -u
PORT=8081
PID=$(ss -ltnp 2>/dev/null | grep ":$PORT" | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)
echo "pid on $PORT: ${PID:-none}"
if [ -n "${PID:-}" ]; then
  kill "$PID"
  sleep 3
fi
if ss -ltn | grep -q ":$PORT"; then
  echo "PORT_BUSY"
else
  echo "PORT_FREE"
fi
ollama stop 'wavecut/vikhr:7b-instruct_0.4-Q4_1' 2>&1 | head -2
free -m | head -2
