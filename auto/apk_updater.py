import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "apk-updater/1.0"
CHUNK = 256 * 1024

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def setup_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def env(name, default=""):
    value = os.environ.get(name)
    return default if value is None else value.strip()


def env_opt(name):
    value = os.environ.get(name)
    return None if value is None else value.strip()


def env_int(name, default):
    try:
        return int(env(name, ""))
    except ValueError:
        return default


def env_bool(name, default=False):
    raw = env(name, "").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    print("[%s] %s" % (now_iso(), message), flush=True)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def save_json(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def sanitize_name(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return cleaned


class Config:
    def __init__(self):
        self.mode = env("MODE", "release").lower()
        self.repo = env("GITHUB_REPO").strip("/")
        self.api_base = env("GH_API_BASE", "https://api.github.com").rstrip("/")
        self.asset_pattern = env("ASSET_PATTERN", "*.apk")
        self.release_tag = env("RELEASE_TAG")
        self.include_prerelease = env_bool("INCLUDE_PRERELEASE", False)
        self.raw_url = env("RAW_URL")
        self.raw_path = env("RAW_PATH")
        self.raw_base = env("RAW_BASE", "https://raw.githubusercontent.com").rstrip("/")
        self.branch = env("GITHUB_BRANCH", "main")
        self.version_url = env("VERSION_URL")
        self.download_dir = env("DOWNLOAD_DIR", "/download")
        self.file_stem = env("FILE_STEM")
        self.stable_name = env_opt("STABLE_NAME")
        self.keep_versions = env_int("KEEP_VERSIONS", 3)
        self.interval = max(env_int("CHECK_INTERVAL_SECONDS", 3600), 30)
        self.run_on_start = env_bool("RUN_ON_START", True)
        self.run_once = env_bool("RUN_ONCE", False)
        self.timeout = env_int("REQUEST_TIMEOUT", 30)
        self.token = env("GITHUB_TOKEN")
        self.state_path = env("STATE_FILE", "/data/state.json")
        self.proxy_prefixes = []
        for item in env("PROXY_PREFIX").split(","):
            item = item.strip()
            if item:
                self.proxy_prefixes.append(item.rstrip("/") + "/")

    def target_desc(self):
        if self.mode == "raw":
            return self.raw_url or "%s/%s/%s/%s" % (
                self.raw_base, self.repo or "?", self.branch, (self.raw_path or "?").lstrip("/"))
        return "%s (%s, pattern=%s, tag=%s)" % (
            self.repo or "?", "prerelease" if self.include_prerelease else "latest",
            self.asset_pattern, self.release_tag or "-")

    def default_stem(self):
        if self.file_stem:
            return self.file_stem
        if self.mode == "raw":
            name = os.path.basename(urllib.parse.urlsplit(self.raw_url or self.raw_path or "").path)
            stem = sanitize_name(os.path.splitext(name)[0])
            return stem or "download"
        return sanitize_name(self.repo.split("/")[-1]) or "release"

    def stable_target(self, ext):
        if self.stable_name is None:
            return self.default_stem() + ext
        return self.stable_name


def url_candidates(url, prefixes):
    result = []
    for prefix in prefixes:
        candidate = prefix + url
        if candidate not in result:
            result.append(candidate)
    if url not in result:
        result.append(url)
    return result


def http_open(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=timeout, context=SSL_CTX)


def github_headers(cfg):
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if cfg.token:
        headers["Authorization"] = "Bearer " + cfg.token
    return headers


def api_get_json(cfg, path):
    url = cfg.api_base + path
    try:
        with http_open(url, headers=github_headers(cfg), timeout=cfg.timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        hint = ""
        if error.code == 403 and "rate limit" in detail.lower():
            hint = " (rate limited, set GITHUB_TOKEN)"
        raise RuntimeError("github api HTTP %s for %s%s %s" % (error.code, path, hint, detail))
    return json.loads(payload.decode("utf-8", "replace"))


def fetch_release(cfg):
    if cfg.release_tag:
        return api_get_json(cfg, "/repos/%s/releases/tags/%s"
                            % (cfg.repo, urllib.parse.quote(cfg.release_tag)))
    if cfg.include_prerelease:
        releases = api_get_json(cfg, "/repos/%s/releases?per_page=10" % cfg.repo)
        releases = [item for item in releases if not item.get("draft")]
        if not releases:
            raise RuntimeError("no releases found for %s" % cfg.repo)
        return releases[0]
    return api_get_json(cfg, "/repos/%s/releases/latest" % cfg.repo)


def pick_asset(release, pattern):
    assets = [item for item in release.get("assets") or [] if item.get("name")]
    for asset in assets:
        if asset["name"].lower() == pattern.lower():
            return asset
    for asset in assets:
        if fnmatch.fnmatch(asset["name"].lower(), pattern.lower()):
            return asset
    return None


def plan_release(cfg, state):
    release = fetch_release(cfg)
    tag = release.get("tag_name") or release.get("name") or "unknown"
    asset = pick_asset(release, cfg.asset_pattern)
    if asset is None:
        names = ", ".join(item.get("name", "?") for item in release.get("assets") or []) or "(none)"
        return {"action": "none", "reason": "no asset matches %s in %s; assets: %s"
                % (cfg.asset_pattern, tag, names)}
    if state.get("asset_id") == asset.get("id") \
            and state.get("asset_updated_at") == asset.get("updated_at") \
            and state.get("tag") == tag:
        return {"action": "none", "reason": "current: %s %s (%s bytes)"
                % (tag, asset["name"], asset.get("size"))}
    urls = url_candidates(asset.get("browser_download_url") or asset.get("url") or "",
                          cfg.proxy_prefixes)
    fallback = None
    if cfg.token and asset.get("id"):
        fallback = "%s/repos/%s/releases/assets/%s" % (cfg.api_base, cfg.repo, asset["id"])
    return {"action": "download", "version": tag, "asset_name": asset["name"],
            "urls": [url for url in urls if url], "expected_size": asset.get("size"),
            "expected_digest": asset.get("digest"), "fallback_url": fallback,
            "description": "%s %s (tag %s, %s bytes)" % (cfg.repo, asset["name"], tag, asset.get("size")),
            "meta": {"asset_id": asset.get("id"), "asset_updated_at": asset.get("updated_at"),
                     "tag": tag, "release_url": release.get("html_url")}}


def raw_target_url(cfg):
    if cfg.raw_url:
        return cfg.raw_url
    if cfg.raw_path and cfg.repo:
        return "%s/%s/%s/%s" % (cfg.raw_base, cfg.repo, cfg.branch, cfg.raw_path.lstrip("/"))
    raise RuntimeError("raw mode needs RAW_URL or GITHUB_REPO + RAW_PATH")


def probe_url(cfg, url):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    last_error = None
    candidates = url_candidates(url, cfg.proxy_prefixes)
    for candidate in candidates:
        try:
            with http_open(candidate, headers=headers, timeout=cfg.timeout) as response:
                meta = {"etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "length": response.headers.get("Content-Length")}
            rotated = [candidate] + [item for item in candidates if item != candidate]
            return meta, rotated
        except Exception as error:
            last_error = "%s: %s" % (type(error).__name__, error)
    raise RuntimeError("probe failed: %s" % last_error)


def fetch_version_label(cfg):
    if not cfg.version_url:
        return ""
    for candidate in url_candidates(cfg.version_url, cfg.proxy_prefixes):
        try:
            with http_open(candidate, headers={"User-Agent": UA}, timeout=cfg.timeout) as response:
                text = response.read(4096).decode("utf-8", "replace")
            for line in text.splitlines():
                line = line.strip()
                if line:
                    return line
        except Exception:
            continue
    return ""


def label_from_last_modified(value):
    if not value:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        stamp = parsedate_to_datetime(value)
        return stamp.strftime("%Y%m%d-%H%M%S")
    except Exception:
        return ""


def plan_raw(cfg, state):
    url = raw_target_url(cfg)
    meta, candidates = probe_url(cfg, url)
    etag = meta.get("etag")
    last_modified = meta.get("last_modified")
    length = None
    if meta.get("length"):
        try:
            length = int(meta["length"])
        except ValueError:
            length = None
    same = False
    if etag and state.get("etag") == etag:
        same = True
    elif last_modified and state.get("last_modified") == last_modified \
            and (length is None or state.get("size") == length):
        same = True
    if same and state.get("sha256"):
        return {"action": "none", "reason": "current: %s (etag/last-modified unchanged)" % url}
    version = fetch_version_label(cfg) or label_from_last_modified(last_modified)
    asset_name = os.path.basename(urllib.parse.urlsplit(url).path) or "download.bin"
    return {"action": "download", "version": version, "asset_name": asset_name,
            "urls": candidates, "expected_size": length, "expected_digest": None,
            "fallback_url": None,
            "description": "%s (%s bytes)" % (url, length),
            "meta": {"etag": etag, "last_modified": last_modified, "url": url}}


def download_once(cfg, url, dest, expected_size=None, expected_digest=None, extra_headers=None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    digest = hashlib.sha256()
    size = 0
    with http_open(url, headers=headers, timeout=cfg.timeout) as response:
        with open(dest, "wb") as handle:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    if expected_size is not None and size != expected_size:
        raise ValueError("size mismatch: got %d want %d" % (size, expected_size))
    if expected_digest:
        want = expected_digest.split(":", 1)[-1].lower()
        got = digest.hexdigest()
        if got != want:
            raise ValueError("sha256 mismatch: got %s want %s" % (got, want))
    return digest.hexdigest(), size


def prune_old(cfg, stem, ext):
    if cfg.keep_versions <= 0:
        return []
    pattern = os.path.join(cfg.download_dir, glob.escape(stem) + "-*" + glob.escape(ext))
    files = [path for path in glob.glob(pattern) if os.path.isfile(path)]
    files.sort(key=os.path.getmtime, reverse=True)
    removed = []
    for path in files[cfg.keep_versions:]:
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
        except OSError:
            pass
    return removed


def apply_download(cfg, state, plan):
    asset_name = plan.get("asset_name") or ""
    ext = os.path.splitext(asset_name)[1] or ".apk"
    stem = cfg.default_stem()
    part = os.path.join(cfg.download_dir, ".%s.download.part" % stem)
    last_error = None
    sha = None
    size = 0
    for url in plan.get("urls") or []:
        try:
            log("downloading %s" % url)
            sha, size = download_once(cfg, url, part, plan.get("expected_size"),
                                      plan.get("expected_digest"))
            break
        except Exception as error:
            last_error = "%s: %s" % (type(error).__name__, error)
            log("download failed (%s), trying next source" % last_error)
            sha = None
    if sha is None and plan.get("fallback_url"):
        try:
            log("downloading via github api (private/authenticated)")
            sha, size = download_once(cfg, plan["fallback_url"], part,
                                      plan.get("expected_size"), plan.get("expected_digest"),
                                      extra_headers={"Accept": "application/octet-stream",
                                                     "Authorization": "Bearer " + cfg.token})
        except Exception as error:
            last_error = "%s: %s" % (type(error).__name__, error)
            sha = None
    if sha is None:
        if os.path.exists(part):
            os.remove(part)
        raise RuntimeError("all sources failed: %s" % last_error)

    if state.get("sha256") == sha:
        os.remove(part)
        log("content identical (sha256 %s), nothing to do" % sha[:12])
        state.update(plan.get("meta") or {})
        return

    version = sanitize_name(plan.get("version")) or ("sha-" + sha[:8])
    target_name = "%s-%s%s" % (stem, version, ext)
    target = os.path.join(cfg.download_dir, target_name)
    if os.path.exists(target):
        version = "%s-%s" % (version, time.strftime("%Y%m%d%H%M%S"))
        target_name = "%s-%s%s" % (stem, version, ext)
        target = os.path.join(cfg.download_dir, target_name)
    os.replace(part, target)
    log("saved %s (%d bytes, sha256 %s)" % (target_name, size, sha[:12]))

    stable = cfg.stable_target(ext)
    if stable:
        stable_path = os.path.join(cfg.download_dir, stable)
        temp = stable_path + ".tmp"
        shutil.copyfile(target, temp)
        os.replace(temp, stable_path)
        log("updated %s" % stable)

    removed = prune_old(cfg, stem, ext)
    if removed:
        log("removed old versions: %s" % ", ".join(removed))

    state.update(plan.get("meta") or {})
    state.update({"last_version": version, "file": target_name, "sha256": sha,
                  "size": size, "downloaded_at_iso": now_iso(),
                  "source": plan.get("description")})


def check_once(cfg, state):
    if cfg.mode == "raw":
        plan = plan_raw(cfg, state)
    else:
        plan = plan_release(cfg, state)
    if plan["action"] == "none":
        log("no update: %s" % plan.get("reason"))
        return
    log("update found: %s" % plan.get("description"))
    apply_download(cfg, state, plan)


def install_signal_handlers():
    def bye(signum, frame):
        raise SystemExit(0)
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, bye)
        except Exception:
            pass


def validate(cfg):
    if cfg.mode not in ("release", "raw"):
        return "invalid MODE=%s (use release or raw)" % cfg.mode
    if cfg.mode == "release" and not cfg.repo:
        return "release mode needs GITHUB_REPO=owner/repo"
    if cfg.mode == "raw" and not (cfg.raw_url or (cfg.repo and cfg.raw_path)):
        return "raw mode needs RAW_URL or GITHUB_REPO + RAW_PATH"
    return ""


def main():
    setup_stdout()
    cfg = Config()
    problem = validate(cfg)
    if problem:
        log("config error: %s" % problem)
        return 2
    os.makedirs(cfg.download_dir, exist_ok=True)
    state = load_json(cfg.state_path) or {}
    log("apk-updater start | mode=%s dir=%s interval=%ss keep=%d" % (
        cfg.mode, cfg.download_dir, cfg.interval, cfg.keep_versions))
    log("target: %s" % cfg.target_desc())
    if cfg.token:
        log("github token: configured")
    if cfg.proxy_prefixes:
        log("proxy prefixes: %s" % ", ".join(cfg.proxy_prefixes))
    install_signal_handlers()
    first = True
    while True:
        if first and not cfg.run_on_start and not cfg.run_once:
            log("RUN_ON_START=0, waiting %ss before first check" % cfg.interval)
        else:
            try:
                check_once(cfg, state)
            except Exception as error:
                log("check failed: %s: %s" % (type(error).__name__, error))
            state["last_check_ts"] = time.time()
            state["last_check_iso"] = now_iso()
            save_json(cfg.state_path, state)
            if cfg.run_once:
                log("RUN_ONCE=1, exiting")
                return 0
        first = False
        time.sleep(cfg.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
