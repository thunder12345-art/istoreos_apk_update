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

if [ -f .env ]; then
    GITHUB_TOKEN="${GITHUB_TOKEN:-$(env_val GITHUB_TOKEN)}"
    GITEE_TOKEN="${GITEE_TOKEN:-$(env_val GITEE_TOKEN)}"
    export GITHUB_TOKEN GITEE_TOKEN
fi

run_py() {
    if command -v "$PY" >/dev/null 2>&1; then
        "$PY" "$@"
    else
        echo "   (host $PY not found, using container image: $IMAGE)"
        docker run --rm -v "$PWD:/app" -w /app -e GITHUB_TOKEN -e GITEE_TOKEN \
            --entrypoint python "${IMAGE}:${TAG}" "$@"
    fi
}

echo "== [0/5] push code to origin + gitee =="
git push origin main || echo "   !! origin push failed (check credentials), continuing"
if git remote get-url gitee >/dev/null 2>&1; then
    git push gitee main || echo "   !! gitee push failed (check credentials/remote), continuing"
else
    echo "   (gitee remote not set; enable once: git remote add gitee https://gitee.com/<user>/<repo>.git)"
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
run_py auto/release.py --tag "$TAG" --dir "$DIST" || echo "   !! release upload had errors (check tokens: GITHUB_TOKEN / GITEE_TOKEN in .env)"

echo ""
echo "done. tag:    $TAG"
echo "      repo:   http://<nas-ip>:8898/  (container unchanged)"
echo "      verify: https://github.com/thunder12345-art/istoreos_apk_update/releases"
echo "              https://gitee.com/thunder0629/istoreos_apk_update/releases"
