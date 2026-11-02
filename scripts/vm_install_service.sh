#!/bin/bash
# Установить backend как systemd-службу и включить автозапуск.
set -e

PID=$(ss -ltnp 2>/dev/null | grep ':8081' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)
echo "manual pid on 8081: ${PID:-none}"
if [ -n "${PID:-}" ]; then
  kill "$PID"
  sleep 4
fi

install -m 0644 /home/ubuntuii/courtdraft-ai/deploy/courtdraft-backend.service \
  /etc/systemd/system/courtdraft-backend.service
systemctl daemon-reload
systemctl enable courtdraft-backend.service
systemctl restart courtdraft-backend.service
sleep 12
systemctl --no-pager --full status courtdraft-backend.service | head -12
echo '--- health ---'
curl -s -m 30 http://127.0.0.1:8081/api/health | head -c 300
echo
