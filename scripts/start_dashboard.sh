#!/data/data/com.termux/files/usr/bin/bash
# Kalshi-Frigo: start the web dashboard (http://localhost:20128/) if it is
# not already serving. Idempotent: exits 0 silently when the port answers.
# The dashboard's own strategy supervisor keeps the DRY lanes running.
REPO="/data/data/com.termux/files/home/Kalshi-Frigo"
PORT="${PORT:-20128}"

# Guard: already serving?
if python3 - "$PORT" <<'EOF'
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    s.close()
EOF
then
    exit 0
fi

command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
cd "$REPO" || exit 1
mkdir -p logs
set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a
export PORT="$PORT"
nohup python3 web_dashboard.py >> logs/dashboard.log 2>&1 &
echo $! > logs/dashboard.pid
