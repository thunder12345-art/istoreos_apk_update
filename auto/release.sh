#!/bin/sh
set -e

cd "$(dirname "$0")/.."

TAG="${1:-v$(date +%Y.%m.%d)}"
IMAGE="${IMAGE:-apk-updater}"
DIST="$PWD/dist"
PY="${PYTHON:-python3}"
export GIT_TERMINAL_PROMPT=0

env_val() {
    sed -n "s/^$1=//p" ./.env 2>/dev/null | tail -n 1 | tr -d '\r' | sed 's/^"//; s/"$//'
}

remote_slug() {
    url="$(git remote get-url "$1" 2>/dev/null || true)"
    case "$url" in
        ""|*".bundle") return 1 ;;
    esac
    printf '%s' "$url" | sed 's/\.git$//' | sed 's#^.*[:/]\([^/:]*/[^/]*\)$#\1#'
}

if [ -f .env ]; then
    GITHUB_TOKEN="${GITHUB_TOKEN:-$(env_val GITHUB_TOKEN)}"
    GITEE_TOKEN="${GITEE_TOKEN:-$(env_val GITEE_TOKEN)}"
    export GITHUB_TOKEN GITEE_TOKEN
fi

GITHUB_SLUG="$(remote_slug origin || true)"
GITEE_SLUG="$(remote_slug gitee || true)"

run_py() {
    if command -v "$PY" >/dev/null 2>&1; then
        "$PY" "$@"
    else
        echo "   (host $PY not found, using container image: $IMAGE)"
        docker run --rm -v "$PWD:/app" -w /app -e GITHUB_TOKEN -e GITEE_TOKEN \
            --entrypoint python "${IMAGE}:${TAG}" "$@"
    fi
}

echo "== [0/5] push code + tag to origin + gitee =="
if ! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null 2>&1; then
    git tag "$TAG"
    echo "   created local tag $TAG"
fi
git push origin main || echo "   !! origin push failed (check credentials), continuing"
git push origin "$TAG" || echo "   !! origin tag push failed, continuing"
if git remote get-url gitee >/dev/null 2>&1; then
    git push gitee main || echo "   !! gitee push failed (check credentials/remote), continuing"
    git push gitee "$TAG" || echo "   !! gitee tag push failed, continuing"
else
    echo "   (gitee not set; enable once: git remote add gitee https://gitee.com/thunder0629/istoreos_apk_update.git)"
fi

echo "== [1/5] build image ${IMAGE}:${TAG} =="
docker build -t "${IMAGE}:${TAG}" -t "${IMAGE}:latest" .

echo "== [2/5] save image bundle =="
mkdir -p "$DIST"
IMAGE_PKG="$DIST/istoreos-apk-update-${TAG}-image.tar.gz"
docker save "${IMAGE}:${TAG}" | gzip > "$IMAGE_PKG"
ls -lh "$IMAGE_PKG"

echo "== [3/5] package install files =="
INSTALL_PKG="$DIST/istoreos-apk-update-${TAG}-install.tar.gz"
tar -czf "$INSTALL_PKG" README.md docker-compose.yml .env.example auto/nas_update.sh auto/release.sh auto/release.py
ls -lh "$INSTALL_PKG"

echo "== [4/5] upload to github + gitee releases =="
set -- --tag "$TAG" --dir "dist"
if [ -n "$GITHUB_SLUG" ]; then set -- "$@" --github "$GITHUB_SLUG"; fi
if [ -n "$GITEE_SLUG" ]; then set -- "$@" --gitee "$GITEE_SLUG"; fi
run_py auto/release.py "$@" || echo "   !! release upload had errors (check tokens: GITHUB_TOKEN / GITEE_TOKEN in .env)"

echo ""
echo "done. tag:    $TAG"
echo "      repo:   http://<nas-ip>:8898/  (container unchanged)"
echo "      verify: https://github.com/thunder12345-art/istoreos_apk_update/releases"
echo "              https://gitee.com/thunder0629/istoreos_apk_update/releases"
