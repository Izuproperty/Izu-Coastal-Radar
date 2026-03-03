#!/usr/bin/env bash
# Sets up a daily cron job to run the price monitor at 09:00 JST (00:00 UTC).
# Run once with:  bash setup_cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
ENV_FILE="$SCRIPT_DIR/.env"
MONITOR="$SCRIPT_DIR/monitor.py"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found."
  echo "Copy .env.example → .env and fill in your credentials first."
  exit 1
fi

# Build the cron line: run at 00:00 UTC = 09:00 JST
CRON_CMD="0 0 * * * set -a; source $ENV_FILE; set +a; $PYTHON $MONITOR >> $SCRIPT_DIR/monitor.log 2>&1"

# Add to crontab only if not already present
( crontab -l 2>/dev/null | grep -qF "$MONITOR" ) && {
  echo "Cron job already exists — nothing to do."
  exit 0
}

( crontab -l 2>/dev/null; echo "$CRON_CMD" ) | crontab -
echo "Cron job installed:"
echo "  $CRON_CMD"
echo ""
echo "It will run every day at 09:00 JST (00:00 UTC)."
echo "Logs will be appended to: $SCRIPT_DIR/monitor.log"
