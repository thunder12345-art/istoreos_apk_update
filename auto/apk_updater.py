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


class ReleaseTarget:
    def __init__(self, tag, patterns, archive, label):
        self.tag = tag
        self.patterns = patterns
        self.archive = archive
        self.label = label


class Config:
    def __init__(self):
        self.mode = env("MODE", "release").lower()
        self.repo = env("GITHUB_REPO").strip("/")
        self.api_base = env("GH_API_BASE", "https://api.github.com").rstrip("/")
        patterns = env("ASSET_PATTERN", "*.apk")
        self.asset_patterns = [item.strip() for item in patterns.split(",") if item.strip()] or ["*.apk"]
        self.release_tag = env("RELEASE_TAG")
        self.include_prerelease = env_bool("INCLUDE_PRERELEASE", False)
        self.targets = [ReleaseTarget(self.release_tag or None, self.asset_patterns, True,
                                      self.release_tag or "latest")]
        for spec in env("EXTRA_TARGETS").split(";"):
            spec = spec.strip()
            if not spec:
                continue
            parts = [part.strip() for part in spec.split("|")]
            tag = parts[0] if parts and parts[0] else None
            pattern_text = parts[1] if len(parts) > 1 else "*.apk"
            archive = True
            if len(parts) > 2:
                archive = parts[2].lower() not in ("0", "false", "no", "off")
            target_patterns = [item.strip() for item in pattern_text.split(",") if item.strip()] or ["*.apk"]
            self.targets.append(ReleaseTarget(tag, target_patterns, archive, tag or "latest"))
        self.raw_url = env("RAW_URL")
        self.raw_path = env("RAW_PATH")
        self.raw_base = env("RAW_BASE", "https://raw.githubusercontent.com").rstrip("/")
        self.branch = env("GITHUB_BRANCH", "main")
        self.version_url = env("VERSION_URL")
        self.download_dir = env("DOWNLOAD_DIR", "/download")
        self.file_stem = env("FILE_STEM")
        stable = env_opt("STABLE_NAME")
        if stable is not None and stable.lower() in ("", "auto"):
            stable = None
        self.stable_name = stable
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
        parts = []
        for target in self.targets:
            parts.append("[%s] tag=%s patterns=%s archive=%d" % (
                target.label, target.tag or "latest", ",".join(target.patterns),
                1 if target.archive else 0))
        return "%s | %s" % (self.repo or "?", " ; ".join(parts))

    def default_stem(self):
        if self.file_stem:
            return self.file_stem
        if self.mode == "raw":
            name = os.path.basename(urllib.parse.urlsplit(self.raw_url or self.raw_path or "").path)
            stem = sanitize_name(os.path.splitext(name)[0])
            return stem or "download"
        return sanitize_name(self.repo.split("/")[-1]) or "release"

    def stem_for(self, asset_name, matched_total):
        if self.file_stem and matched_total == 1:
            return self.file_stem
        stem = sanitize_name(os.path.splitext(asset_name)[0])
        return stem or "download"

    def stable_for(self, asset_name, matched_total):
        if self.stable_name is None:
            return asset_name
        if self.stable_name == "":
            return ""
        if matched_total == 1:
            return self.stable_name
        return asset_name


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


def fetch_release(cfg, tag=None):
    if tag:
        return api_get_json(cfg, "/repos/%s/releases/tags/%s"
                            % (cfg.repo, urllib.parse.quote(tag)))
    releases = api_get_json(cfg, "/repos/%s/releases?per_page=20" % cfg.repo)
    excluded = {target.tag for target in cfg.targets if target.tag}
    candidates = []
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not cfg.include_prerelease:
            continue
        if (release.get("tag_name") or "") in excluded:
            continue
        candidates.append(release)
    if not candidates:
        raise RuntimeError("no usable release found for %s" % cfg.repo)
    candidates.sort(key=lambda item: (item.get("published_at") or item.get("created_at") or "",
                                      item.get("id") or 0), reverse=True)
    return candidates[0]


def version_label(release):
    for value in (release.get("tag_name"), release.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def pick_assets(release, patterns):
    assets = [item for item in release.get("assets") or [] if item.get("name")]
    matched = []
    seen = set()
    for pattern in patterns:
        wildcard = pattern.lower()
        for asset in assets:
            if asset["id"] in seen:
                continue
            if fnmatch.fnmatch(asset["name"].lower(), wildcard):
                matched.append(asset)
                seen.add(asset["id"])
    return matched


def previous_entry(assets_state, key, name):
    if key in assets_state:
        return key, assets_state[key]
    if name and name in assets_state:
        return name, assets_state[name]
    return key, {}


def plan_release(cfg, state, target):
    release = fetch_release(cfg, target.tag)
    tag = version_label(release) or target.tag or "unknown"
    matched = pick_assets(release, target.patterns)
    if not matched:
        names = ", ".join(item.get("name", "?") for item in release.get("assets") or []) or "(none)"
        return {"action": "none", "reason": "[%s] no asset matches %s in %s; assets: %s"
                % (target.label, ",".join(target.patterns), tag, names)}
    assets_state = state.get("assets") or {}
    items = []
    current = []
    for asset in matched:
        key = "%s:%s" % (tag, asset["name"])
        _, previous = previous_entry(assets_state, key, asset["name"])
        if previous.get("asset_id") == asset.get("id") \
                and previous.get("asset_updated_at") == asset.get("updated_at") \
                and previous.get("tag") == tag:
            current.append(asset["name"])
            continue
        urls = url_candidates(asset.get("browser_download_url") or asset.get("url") or "",
                              cfg.proxy_prefixes)
        fallback = None
        if cfg.token and asset.get("id"):
            fallback = "%s/repos/%s/releases/assets/%s" % (cfg.api_base, cfg.repo, asset["id"])
        items.append({"asset_name": asset["name"], "state_key": key,
                      "version": version_label(release),
                      "urls": [url for url in urls if url],
                      "expected_size": asset.get("size"), "expected_digest": asset.get("digest"),
                      "fallback_url": fallback,
                      "archive": target.archive, "matched_total": len(matched),
                      "meta": {"asset_id": asset.get("id"),
                               "asset_updated_at": asset.get("updated_at"),
                               "tag": tag, "url": asset.get("browser_download_url")}})
    if not items:
        return {"action": "none", "reason": "[%s] current: %s %s (%s)"
                % (target.label, cfg.repo, tag, ", ".join(current))}
    return {"action": "download", "items": items,
            "description": "[%s] %s tag %s -> %s" % (target.label, cfg.repo, tag,
                                                     ", ".join(item["asset_name"] for item in items)),
            "tag": tag, "release_url": release.get("html_url")}


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
    name = os.path.basename(urllib.parse.urlsplit(url).path) or "download.bin"
    key = "raw:%s" % name
    assets_state = state.get("assets") or {}
    _, previous = previous_entry(assets_state, key, name)
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
    if etag and previous.get("etag") == etag:
        same = True
    elif last_modified and previous.get("last_modified") == last_modified \
            and (length is None or previous.get("size") == length):
        same = True
    if same and previous.get("sha256"):
        return {"action": "none", "reason": "[raw] current: %s (etag/last-modified unchanged)" % url}
    version = fetch_version_label(cfg) or label_from_last_modified(last_modified)
    return {"action": "download", "matched_total": 1,
            "items": [{"asset_name": name, "state_key": key, "version": version,
                       "urls": candidates, "expected_size": length, "expected_digest": None,
                       "fallback_url": None, "archive": True, "matched_total": 1,
                       "meta": {"etag": etag, "last_modified": last_modified, "url": url}}],
            "description": "[raw] %s (%s bytes)" % (url, length)}


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


def fetch_item(cfg, item, part):
    last_error = None
    for url in item.get("urls") or []:
        try:
            log("downloading %s" % url)
            sha, size = download_once(cfg, url, part, item.get("expected_size"),
                                      item.get("expected_digest"))
            return sha, size, None
        except Exception as error:
            last_error = "%s: %s" % (type(error).__name__, error)
            log("download failed (%s), trying next source" % last_error)
    if item.get("fallback_url"):
        try:
            log("downloading via github api (private/authenticated)")
            sha, size = download_once(cfg, item["fallback_url"], part,
                                      item.get("expected_size"), item.get("expected_digest"),
                                      extra_headers={"Accept": "application/octet-stream",
                                                     "Authorization": "Bearer " + cfg.token})
            return sha, size, None
        except Exception as error:
            last_error = "%s: %s" % (type(error).__name__, error)
    return None, 0, last_error


def apply_download(cfg, state, plan):
    assets_state = state.setdefault("assets", {})
    stable_warned = False
    errors = []
    for item in plan["items"]:
        key = item["state_key"]
        name = item["asset_name"]
        archive = item.get("archive", True)
        matched_total = item.get("matched_total") or 1
        old_key, previous = previous_entry(assets_state, key, name)
        ext = os.path.splitext(name)[1] or ".apk"
        stem = cfg.stem_for(name, matched_total)
        part = os.path.join(cfg.download_dir, ".%s.download.part" % (stem if archive else name))
        sha, size, last_error = fetch_item(cfg, item, part)
        if sha is None:
            if os.path.exists(part):
                os.remove(part)
            errors.append("%s: %s" % (name, last_error))
            continue

        if previous.get("sha256") == sha:
            os.remove(part)
            log("%s: content identical (sha256 %s), nothing to do" % (name, sha[:12]))
            merged = {field: previous[field] for field in
                      ("version", "file", "sha256", "size", "downloaded_at_iso")
                      if field in previous}
            merged.update(item["meta"])
            assets_state[key] = merged
            if old_key != key:
                assets_state.pop(old_key, None)
            continue

        version = ""
        if archive:
            version = sanitize_name(item.get("version")) or ("sha-" + sha[:8])
            target_name = "%s-%s%s" % (stem, version, ext)
            target = os.path.join(cfg.download_dir, target_name)
            if os.path.exists(target):
                version = "%s-%s" % (version, time.strftime("%Y%m%d%H%M%S"))
                target_name = "%s-%s%s" % (stem, version, ext)
                target = os.path.join(cfg.download_dir, target_name)
        else:
            target_name = name
            target = os.path.join(cfg.download_dir, target_name)
        os.replace(part, target)
        log("saved %s (%d bytes, sha256 %s)" % (target_name, size, sha[:12]))

        if archive:
            stable = cfg.stable_for(name, matched_total)
            if stable:
                if cfg.stable_name and stable != cfg.stable_name and not stable_warned:
                    log("STABLE_NAME ignored: %d assets matched, keeping original filenames"
                        % matched_total)
                    stable_warned = True
                stable_path = os.path.join(cfg.download_dir, stable)
                temp = stable_path + ".tmp"
                shutil.copyfile(target, temp)
                os.replace(temp, stable_path)
                log("updated %s" % stable)

            removed = prune_old(cfg, stem, ext)
            if removed:
                log("removed old versions: %s" % ", ".join(removed))

        result = dict(item["meta"])
        result.update({"version": version, "file": target_name, "sha256": sha,
                       "size": size, "downloaded_at_iso": now_iso()})
        assets_state[key] = result
        if old_key != key:
            assets_state.pop(old_key, None)

    if errors:
        raise RuntimeError("failed: " + "; ".join(errors))


def check_once(cfg, state):
    if cfg.mode == "raw":
        jobs = [("raw", lambda: plan_raw(cfg, state))]
    else:
        jobs = [("release:%s" % target.label,
                 (lambda target=target: plan_release(cfg, state, target)))
                for target in cfg.targets]
    errors = []
    for label, planner in jobs:
        try:
            plan = planner()
            if plan["action"] == "none":
                log("no update: %s" % plan.get("reason"))
                continue
            log("update found: %s" % plan.get("description"))
            apply_download(cfg, state, plan)
            if plan.get("tag"):
                state["tag"] = plan["tag"]
            if plan.get("release_url"):
                state["release_url"] = plan["release_url"]
        except Exception as error:
            errors.append("%s: %s: %s" % (label, type(error).__name__, error))
    if errors:
        raise RuntimeError("; ".join(errors))


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
