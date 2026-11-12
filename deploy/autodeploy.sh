#!/usr/bin/env bash
# Auto-deploy for the BTD bot.
# Runs from cron on the VPS: if GitHub has new commits on this branch,
# pull them and rebuild the container. Does nothing when there's no change.
# Only pulls (outbound); nothing on the VPS is exposed to the internet.
set -euo pipefail
cd "$(dirname "$0")/.."

# Don't let two runs overlap if a build takes longer than a minute.
exec 9>/tmp/btd-autodeploy.lock
flock -n 9 || exit 0

git fetch --quiet origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse '@{u}')
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "$(date -Is) deploying ${REMOTE:0:7}"
git merge --ff-only --quiet '@{u}'
docker compose up -d --build
docker image prune -f >/dev/null
echo "$(date -Is) done"
