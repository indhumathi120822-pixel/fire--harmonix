#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
python3 -c "from backend.database import init_db; init_db()"
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8003
