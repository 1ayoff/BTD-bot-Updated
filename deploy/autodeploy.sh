#!/usr/bin/env bash
# auto-deploy for the btd bot.
# cron runs this every minute on the vps. if github has new commits it pulls
# them and rebuilds the container. if a build fails it tries again next minute
# instead of giving up, and it only pulls - nothing on the vps is exposed.
set -euo pipefail
cd "$(dirname "$0")/.."

# don't let two runs overlap if a build takes longer than a minute
exec 9>/tmp/btd-autodeploy.lock
flock -n 9 || exit 0

git fetch --quiet origin
if [ "$(git rev-parse HEAD)" != "$(git rev-parse '@{u}')" ]; then
  git merge --ff-only --quiet '@{u}'
fi

# remember what's actually running so a failed build gets retried
STATE=.git/deployed-commit
TARGET=$(git rev-parse HEAD)
[ "$(cat "$STATE" 2>/dev/null || true)" = "$TARGET" ] && exit 0

echo "$(date -Is) deploying ${TARGET:0:7}"
docker compose up -d --build
docker image prune -f >/dev/null
echo "$TARGET" > "$STATE"
echo "$(date -Is) done"
