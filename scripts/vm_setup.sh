#!/bin/bash
set -e
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/limits.conf <<'EOF'
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=10m"
EOF
systemctl daemon-reload
systemctl restart ollama
sleep 3
systemctl is-active ollama
curl -s http://127.0.0.1:11434/api/version
