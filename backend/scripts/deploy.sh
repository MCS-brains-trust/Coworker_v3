#!/usr/bin/env bash
set -euo pipefail

# Run from local workstation. Usage: ./deploy.sh [git-sha-or-tag]
RELEASE="${1:-$(git rev-parse --short HEAD)}"
HOST="coworker-v3"
RELEASE_DIR="/opt/coworker/releases/$RELEASE"
DOMAIN="coworker.mcands.com.au"

echo "Deploying $RELEASE to $HOST..."

# Sync backend code to droplet
ssh -p 2202 $HOST "sudo -u coworker mkdir -p $RELEASE_DIR/backend"
rsync -az --delete -e "ssh -p 2202" \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude '__pycache__' --exclude '*.pyc' \
  ./backend/ $HOST:$RELEASE_DIR/backend/

# Install Python deps and run migrations on droplet
ssh -p 2202 $HOST "sudo -u coworker bash <<INNER
  cd $RELEASE_DIR/backend
  uv sync --python python3.12
  uv run alembic upgrade head
INNER"

# Atomic switch via symlink
ssh -p 2202 $HOST "sudo ln -sfn $RELEASE_DIR /opt/coworker/current && \
  sudo systemctl reload-or-restart coworker-api"

# Smoke test
sleep 3
curl -fsS "https://${DOMAIN}/health" | python3 -m json.tool
echo ""
echo "Deployed $RELEASE successfully."
