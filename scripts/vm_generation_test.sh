#!/bin/bash
# Реальная генерация проекта на Vikhr 7B: время и память.
set -u
CASE=72a3ed02-99cc-4cb2-84b7-d31f8cded105
BASE=http://127.0.0.1:8081

echo "--- before ---"
free -m | head -3
date +%s > /tmp/gen_start

JOB=$(curl -s -m 30 -X POST "$BASE/api/cases/$CASE/draft" \
  -H 'Content-Type: application/json' -d '{"actor":"judge"}')
echo "submit: $JOB"
ID=$(echo "$JOB" | sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')
if [ -z "$ID" ]; then
  echo "NO JOB ID"
  exit 1
fi

for i in $(seq 1 120); do
  sleep 10
  STATE=$(curl -s -m 20 "$BASE/api/jobs/$ID")
  NOW=$(date +%s)
  ELAPSED=$((NOW - $(cat /tmp/gen_start)))
  echo "[$ELAPSED s] $(echo "$STATE" | head -c 300)"
  echo "$STATE" | grep -qE '"state":"(DONE|FAILED)"' && break
  if [ $((i % 3)) -eq 0 ]; then
    free -m | awk 'NR==2{print "    mem used="$3" free="$4" avail="$7}'
  fi
done

echo "--- after ---"
free -m | head -3
END=$(date +%s)
echo "TOTAL SECONDS: $((END - $(cat /tmp/gen_start)))"
echo "--- ollama ps ---"
ollama ps
echo "--- last 30 log lines ---"
tail -30 ~/backend.log
