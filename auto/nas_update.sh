#!/bin/sh
set -e

cd "$(dirname "$0")/.."

BUNDLE="${1:-/mnt/sata1-2/docker/apk-updater.bundle}"
TS="$(date +%Y%m%d-%H%M%S)"
export GIT_TERMINAL_PROMPT=0

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

echo "== [0/6] ensure git identity =="
git config user.email >/dev/null 2>&1 || git config user.email "nas@local"
git config user.name >/dev/null 2>&1 || git config user.name "iStoreOS NAS"
echo "   identity: $(git config user.name) <$(git config user.email)>"

echo "== [1/6] stop container =="
docker stop apk-updater >/dev/null 2>&1 || true

echo "== [2/6] snapshot local changes =="
git add -A
if git diff --cached --quiet; then
    echo "   (nothing to commit)"
else
    git commit -m "local snapshot $TS"
fi

echo "== [3/6] fetch bundle: $BUNDLE =="
if [ ! -f "$BUNDLE" ]; then
    echo "bundle not found: $BUNDLE"
    docker start apk-updater >/dev/null 2>&1 || true
    exit 1
fi
git fetch "$BUNDLE" "+refs/heads/main:refs/heads/incoming"
git merge --no-commit -X theirs incoming
if git ls-files -u | grep -q .; then
    echo "unresolved conflicts, aborting merge:"
    git diff --name-only --diff-filter=U
    git merge --abort || true
    docker start apk-updater >/dev/null 2>&1 || true
    exit 1
fi

echo "== [4/6] commit + push =="
git add -A
if git diff --cached --quiet; then
    echo "   (nothing to commit)"
else
    git commit -m "sync upstream $TS"
fi
REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
case "$REMOTE_URL" in
    ""|*".bundle")
        echo "   (origin is local/none, skip push)" ;;
    *)
        git push origin main || echo "   !! push failed (check credentials), local commit is safe" ;;
esac
if git remote get-url gitee >/dev/null 2>&1; then
    git push gitee main || echo "   !! gitee push failed (check credentials/remote), local commit is safe"
else
    echo "   (gitee remote not set; enable once: git remote add gitee https://gitee.com/<user>/<repo>.git)"
fi

echo "== [5/6] rebuild image =="
compose build

echo "== [6/6] start container =="
compose up -d

echo ""
echo "done. logs:  docker logs --tail 30 apk-updater"
echo "      files: ls -l <APK_DIR>"
