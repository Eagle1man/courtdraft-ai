#!/bin/bash
# Задать Ollama 4 потока (в ВМ 4 vCPU, автоопределение берёт 2).
set -e
cat > /etc/systemd/system/ollama.service.d/limits.conf <<'EOF'
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=10m"
Environment="OLLAMA_NUM_THREAD=4"
EOF
systemctl daemon-reload
systemctl restart ollama
sleep 5
systemctl is-active ollama
curl -s http://127.0.0.1:11434/api/version
