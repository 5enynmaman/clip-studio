#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Сначала создайте .venv и установите зависимости по README.md"
  exit 1
fi
if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm ci && npm run build)
fi
.venv/bin/python -c 'from backend import media; import subprocess; r=subprocess.run([media.FFMPEG,"-hide_banner","-filters"],capture_output=True,text=True,check=True); missing=[f for f in ["drawtext","subtitles","zscale"] if f not in r.stdout]; assert not missing, "Нужен FFmpeg с фильтрами: "+", ".join(missing)'
.venv/bin/python -m backend.worker &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' EXIT INT TERM
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
