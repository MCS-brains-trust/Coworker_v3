#!/bin/bash
# Registers a cron job to run main.py at 7 AM every weekday (Mon-Fri).
# Run once on the droplet after cloning the repo.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"
CRON_LINE="0 7 * * 1-5 cd $SCRIPT_DIR && $PYTHON main.py"

# Add to crontab without duplicating
( crontab -l 2>/dev/null | grep -v "prefill_returns/main.py"; echo "$CRON_LINE" ) | crontab -

echo "Cron job registered:"
echo "  $CRON_LINE"
echo ""
echo "To verify: crontab -l"
echo "To check logs: tail -f $SCRIPT_DIR/prefill_returns.log"
