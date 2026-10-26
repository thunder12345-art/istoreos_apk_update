import copy
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "apk-updater/1.0"
CHUNK = 256 * 1024
BASE = os.path.dirname(os.path.abspath(__file__))
PANEL_FILE = os.path.join(BASE, "panel.html")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

LOG_LINES = deque(maxlen=400)


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
    line = "[%s] %s" % (now_iso(), message)
    LOG_LINES.append(line)
    print(line, flush=True)
    path = env("LOG_FILE", "/data/updater.log")
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(list(LOG_LINES)[-200:]) + "\n")
        else:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        pass


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


def parse_prefixes(text):
    result = []
    for item in str(text or "").split(","):
        item = item.strip()
        if item:
            result.append(item.rstrip("/") + "/")
    return result


def parse_hour(value):
    text = str(value if value is not None else "").strip()
    if text == "":
        return None
    try:
        hour = int(text)
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


_SKELETON_RE = re.compile(
    r"v?\d+(?:\.\d+)+(?![A-Za-z0-9])|(?<![A-Za-z0-9])\d+(?![A-Za-z0-9])")


def name_skeleton(name):
    return _SKELETON_RE.sub("#", str(name or "").lower())


class ReleaseTarget:
    def __init__(self, repo, tag, patterns, archive, subdir=None, cleanup=None):
        self.repo = repo or ""
        self.tag = tag
        self.patterns = patterns
        self.archive = archive
        self.subdir = subdir or ""
        self.cleanup = [item for item in (cleanup or []) if item]
        self.label = "%s:%s" % (self.repo or "?", tag or "latest")

    def effective_subdir(self):
        if self.subdir:
            return self.subdir
        base = sanitize_name(self.repo.replace("/", "-"))
        if self.tag:
            base = "%s-%s" % (base, sanitize_name(self.tag))
        return base or "download"

    def relabel(self):
        self.label = "%s:%s" % (self.repo or "?", self.tag or "latest")
        return self


class Config:
    def __init__(self, data=None):
        self.mode = env("MODE", "release").lower()
        self.repo = env("GITHUB_REPO").strip("/")
        self.api_base = env("GH_API_BASE", "https://api.github.com").rstrip("/")
        patterns = env("ASSET_PATTERN", "*.apk")
        self.asset_patterns = [item.strip() for item in patterns.split(",") if item.strip()] or ["*.apk"]
        self.release_tag = env("RELEASE_TAG")
        self.include_prerelease = env_bool("INCLUDE_PRERELEASE", False)
        self.targets = [ReleaseTarget(self.repo, self.release_tag or None, self.asset_patterns, True)]
        for spec in env("EXTRA_TARGETS").split(";"):
            spec = spec.strip()
            if not spec:
                continue
            parts = [part.strip() for part in spec.split("|")]
            cleanup_text = ""
            if len(parts) >= 5:
                repo, tag, pattern_text, archive_text, cleanup_text = parts[:5]
            elif len(parts) >= 4:
                repo, tag, pattern_text, archive_text = parts[:4]
            else:
                repo = ""
                tag = parts[0] if parts else ""
                pattern_text = parts[1] if len(parts) > 1 else "*.apk"
                archive_text = parts[2] if len(parts) > 2 else "1"
            archive = archive_text.lower() not in ("0", "false", "no", "off")
            target_patterns = [item.strip() for item in pattern_text.split(",") if item.strip()] or ["*.apk"]
            cleanup_patterns = [item.strip() for item in cleanup_text.split(",") if item.strip()]
            self.targets.append(ReleaseTarget(repo or self.repo, tag or None,
                                              target_patterns, archive, None,
                                              cleanup_patterns))
        self.raw_url = env("RAW_URL")
        self.raw_path = env("RAW_PATH")
        self.raw_base = env("RAW_BASE", "https://raw.githubusercontent.com").rstrip("/")
        self.branch = env("GITHUB_BRANCH", "main")
        self.version_url = env("VERSION_URL")
        self.download_dir = env("DOWNLOAD_DIR", "/nas/TV")
        self.file_stem = env("FILE_STEM")
        stable = env_opt("STABLE_NAME")
        if stable is not None and stable.lower() in ("", "auto"):
            stable = None
        self.stable_name = stable
        self.keep_versions = env_int("KEEP_VERSIONS", 1)
        self.interval_days = env_int("CHECK_INTERVAL_DAYS", -1)
        if self.interval_days < 0:
            seconds = max(0, env_int("CHECK_INTERVAL_SECONDS", 86400))
            self.interval_days = 0 if seconds <= 0 else max(1, int(round(seconds / 86400.0)))
        self.check_hour = parse_hour(env("CHECK_HOUR"))
        self.run_on_start = env_bool("RUN_ON_START", True)
        self.run_once = env_bool("RUN_ONCE", False)
        self.timeout = env_int("REQUEST_TIMEOUT", 30)
        self.token = env("GITHUB_TOKEN")
        self.state_path = env("STATE_FILE", "/data/state.json")
        self.proxy_prefixes = parse_prefixes(env("PROXY_PREFIX"))
        if data:
            self.apply_overrides(data)

    def apply_overrides(self, data):
        if not isinstance(data, dict):
            return
        if data.get("repo"):
            new_repo = str(data["repo"]).strip().strip("/")
            if new_repo != self.repo:
                for target in self.targets:
                    if not target.repo or target.repo == self.repo:
                        target.repo = new_repo
            self.repo = new_repo
        if data.get("download_dir"):
            self.download_dir = str(data["download_dir"]).strip()
        if data.get("check_interval_days") is not None:
            try:
                self.interval_days = max(0, int(data["check_interval_days"]))
            except (TypeError, ValueError):
                pass
        elif data.get("check_interval_seconds") is not None:
            try:
                seconds = max(0, int(data["check_interval_seconds"]))
                self.interval_days = 0 if seconds <= 0 else max(1, int(round(seconds / 86400.0)))
            except (TypeError, ValueError):
                pass
        if "check_hour" in data:
            self.check_hour = parse_hour(data.get("check_hour"))
        if data.get("keep_versions") is not None:
            try:
                self.keep_versions = max(int(data["keep_versions"]), 0)
            except (TypeError, ValueError):
                pass
        if "stable_name" in data:
            value = data.get("stable_name")
            value = "auto" if value is None else str(value).strip()
            self.stable_name = None if value.lower() in ("", "auto") else value
        if "proxy_prefix" in data:
            self.proxy_prefixes = parse_prefixes(data.get("proxy_prefix"))
        if "github_token" in data:
            self.token = str(data.get("github_token") or "").strip()
        if "include_prerelease" in data:
            self.include_prerelease = bool(data.get("include_prerelease"))
        if isinstance(data.get("targets"), list) and data["targets"]:
            targets = []
            for item in data["targets"]:
                if not isinstance(item, dict):
                    continue
                repo = str(item.get("repo") or "").strip().strip("/") or self.repo
                tag = str(item.get("tag") or "").strip() or None
                pats = item.get("patterns")
                if isinstance(pats, str):
                    pats = [part.strip() for part in pats.split(",")]
                pats = [str(part).strip() for part in (pats or []) if str(part).strip()] or ["*.apk"]
                subdir = str(item.get("subdir") or "").strip()
                cleanup = item.get("cleanup")
                if isinstance(cleanup, str):
                    cleanup = [part.strip() for part in cleanup.split(",")]
                cleanup = [str(part).strip() for part in (cleanup or []) if str(part).strip()]
                targets.append(ReleaseTarget(repo, tag, pats, bool(item.get("archive", True)),
                                             subdir, cleanup))
            if targets:
                self.targets = targets
        for target in self.targets:
            if not target.repo:
                target.repo = self.repo
            target.relabel()

    def to_config_dict(self):
        return {
            "repo": self.repo,
            "download_dir": self.download_dir,
            "check_interval_days": self.interval_days,
            "check_hour": "" if self.check_hour is None else self.check_hour,
            "keep_versions": self.keep_versions,
            "stable_name": "auto" if self.stable_name is None else self.stable_name,
            "proxy_prefix": ",".join(prefix.rstrip("/") for prefix in self.proxy_prefixes),
            "github_token": self.token,
            "include_prerelease": self.include_prerelease,
            "targets": [{"repo": target.repo, "tag": target.tag or "",
                         "patterns": list(target.patterns), "archive": target.archive,
                         "subdir": target.effective_subdir(),
                         "cleanup": list(target.cleanup)} for target in self.targets],
            "mode": self.mode,
        }

    def target_subdir(self, target):
        return target.effective_subdir()

    def schedule_desc(self):
        if self.interval_days <= 0:
            return "手动"
        desc = "每%d天" % self.interval_days
        if self.check_hour is not None:
            desc += " %02d:00" % self.check_hour
        return desc

    def target_dir(self, target):
        subdir = target.effective_subdir()
        return os.path.join(self.download_dir, subdir) if subdir else self.download_dir

    def target_desc(self):
        if self.mode == "raw":
            return self.raw_url or "%s/%s/%s/%s" % (
                self.raw_base, self.repo or "?", self.branch, (self.raw_path or "?").lstrip("/"))
        parts = []
        for target in self.targets:
            parts.append("[%s] tag=%s patterns=%s archive=%d dir=%s" % (
                target.label, target.tag or "latest", ",".join(target.patterns),
                1 if target.archive else 0, target.effective_subdir()))
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


def fetch_release(cfg, repo, tag=None):
    if tag:
        return api_get_json(cfg, "/repos/%s/releases/tags/%s"
                            % (repo, urllib.parse.quote(tag)))
    releases = api_get_json(cfg, "/repos/%s/releases?per_page=20" % repo)
    excluded = {target.tag for target in cfg.targets if target.tag and target.repo == repo}
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
        raise RuntimeError("no usable release found for %s" % repo)
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
    repo = target.repo or cfg.repo
    release = fetch_release(cfg, repo, target.tag)
    tag = version_label(release) or target.tag or "unknown"
    subdir = cfg.target_subdir(target)
    matched = pick_assets(release, target.patterns)
    if not matched:
        names = ", ".join(item.get("name", "?") for item in release.get("assets") or []) or "(none)"
        return {"action": "none", "tag": tag,
                "reason": "[%s] no asset matches %s in %s; assets: %s"
                % (target.label, ",".join(target.patterns), tag, names)}
    assets_state = state.get("assets") or {}
    items = []
    current = []
    for asset in matched:
        key = "%s:%s:%s" % (repo, tag, asset["name"])
        _, previous = previous_entry(assets_state, key, asset["name"])
        if previous.get("asset_id") == asset.get("id") \
                and previous.get("asset_updated_at") == asset.get("updated_at") \
                and previous.get("tag") == tag:
            probe = {"asset_name": asset["name"], "archive": target.archive,
                     "matched_total": len(matched), "subdir": subdir}
            if files_present(cfg, probe, previous):
                current.append(asset["name"])
                continue
        urls = url_candidates(asset.get("browser_download_url") or asset.get("url") or "",
                              cfg.proxy_prefixes)
        fallback = None
        if cfg.token and asset.get("id"):
            fallback = "%s/repos/%s/releases/assets/%s" % (cfg.api_base, repo, asset["id"])
        items.append({"asset_name": asset["name"], "state_key": key,
                      "version": version_label(release),
                      "urls": [url for url in urls if url],
                      "expected_size": asset.get("size"), "expected_digest": asset.get("digest"),
                      "fallback_url": fallback,
                      "archive": target.archive, "matched_total": len(matched),
                      "subdir": subdir,
                      "meta": {"asset_id": asset.get("id"),
                               "asset_updated_at": asset.get("updated_at"),
                               "tag": tag, "repo": repo,
                               "url": asset.get("browser_download_url")}})
    if not items:
        return {"action": "none", "tag": tag,
                "asset_names": [asset["name"] for asset in matched],
                "reason": "[%s] current: %s %s (%s)"
                % (target.label, repo, tag, ", ".join(current))}
    return {"action": "download", "items": items,
            "asset_names": [asset["name"] for asset in matched],
            "description": "[%s] %s tag %s -> %s" % (target.label, repo, tag,
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
    if same and previous.get("sha256") \
            and files_present(cfg, {"asset_name": name, "archive": True, "matched_total": 1,
                                    "subdir": ""}, previous):
        return {"action": "none", "reason": "[raw] current: %s (etag/last-modified unchanged)" % url}
    version = fetch_version_label(cfg) or label_from_last_modified(last_modified)
    return {"action": "download", "matched_total": 1,
            "items": [{"asset_name": name, "state_key": key, "version": version,
                       "urls": candidates, "expected_size": length, "expected_digest": None,
                       "fallback_url": None, "archive": True, "matched_total": 1,
                       "subdir": "",
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


def item_dir(cfg, item):
    subdir = item.get("subdir") or ""
    return os.path.join(cfg.download_dir, subdir) if subdir else cfg.download_dir


def prune_old(cfg, work_dir, stem, ext):
    if cfg.keep_versions <= 0:
        return []
    pattern = os.path.join(work_dir, glob.escape(stem) + "-*" + glob.escape(ext))
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


def remove_versioned_files(cfg, work_dir, name):
    stems = set()
    stem = sanitize_name(os.path.splitext(name)[0])
    if stem:
        stems.add(stem)
    if cfg.file_stem:
        stems.add(sanitize_name(cfg.file_stem))
    ext = os.path.splitext(name)[1] or ".apk"
    removed = []
    for stem in stems:
        pattern = os.path.join(work_dir, glob.escape(stem) + "-*" + glob.escape(ext))
        for path in glob.glob(pattern):
            if os.path.isfile(path) and os.path.basename(path) != name:
                try:
                    os.remove(path)
                    removed.append(os.path.basename(path))
                except OSError:
                    pass
    return removed


def cleanup_target_versioned(cfg, state, target):
    assets_state = state.get("assets") or {}
    repo = target.repo or cfg.repo
    work_dir = cfg.target_dir(target)
    if not os.path.isdir(work_dir):
        return []
    removed = []
    for key, entry in assets_state.items():
        if key.startswith("raw:"):
            continue
        if is_new_state_key(key) and not key.startswith(repo + ":"):
            continue
        name = key.rsplit(":", 1)[-1] if ":" in key else key
        if not any(fnmatch.fnmatch(name.lower(), pattern.lower())
                   for pattern in target.patterns):
            continue
        removed.extend(remove_versioned_files(cfg, work_dir, name))
        if entry.get("file") != name and os.path.isfile(os.path.join(work_dir, name)):
            entry["file"] = name
    return removed


def stale_group_keys(target, name):
    if target.cleanup:
        return [pattern for pattern in target.cleanup
                if fnmatch.fnmatch(name.lower(), pattern.lower())]
    return [name_skeleton(name)]


def cleanup_stale_names(cfg, state, target, repo, current_names):
    current = set(current_names)
    current_groups = set()
    for name in current_names:
        current_groups.update(stale_group_keys(target, name))
    if not current_groups:
        return []
    assets_state = state.get("assets") or {}
    work_dir = cfg.target_dir(target)
    removed = []
    for key, entry in list(assets_state.items()):
        if key.startswith("raw:"):
            continue
        name = key.rsplit(":", 1)[-1] if ":" in key else key
        if name in current:
            continue
        if is_new_state_key(key) and key.split(":", 1)[0] != repo:
            continue
        if not any(fnmatch.fnmatch(name.lower(), pattern.lower())
                   for pattern in target.patterns):
            continue
        if not (set(stale_group_keys(target, name)) & current_groups):
            continue
        for file_name in {entry.get("file"), name}:
            if not file_name or file_name in current:
                continue
            path = os.path.join(work_dir, file_name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed.append(file_name)
                except OSError:
                    pass
        assets_state.pop(key, None)
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


def files_present(cfg, item, previous):
    name = item["asset_name"]
    work_dir = item_dir(cfg, item)
    if not item.get("archive", True):
        return os.path.exists(os.path.join(work_dir, name))
    file_name = previous.get("file")
    if not file_name or not os.path.exists(os.path.join(work_dir, file_name)):
        return False
    stable = cfg.stable_for(name, item.get("matched_total") or 1)
    if stable and not os.path.exists(os.path.join(work_dir, stable)):
        return False
    return True


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
        work_dir = item_dir(cfg, item)
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError as error:
            errors.append("%s: cannot create %s: %s" % (name, work_dir, error))
            continue
        ext = os.path.splitext(name)[1] or ".apk"
        stem = cfg.stem_for(name, matched_total)
        part = os.path.join(work_dir, ".%s.download.part" % (stem if archive else name))
        sha, size, last_error = fetch_item(cfg, item, part)
        if sha is None:
            if os.path.exists(part):
                os.remove(part)
            errors.append("%s: %s" % (name, last_error))
            continue

        if previous.get("sha256") == sha and files_present(cfg, item, previous):
            os.remove(part)
            log("%s: content identical (sha256 %s), nothing to do" % (name, sha[:12]))
            if not archive:
                removed = remove_versioned_files(cfg, work_dir, name)
                if removed:
                    log("removed old versions: %s" % ", ".join(removed))
            merged = {field: previous[field] for field in
                      ("version", "file", "sha256", "size", "downloaded_at_iso")
                      if field in previous}
            merged.update(item["meta"])
            if not archive:
                merged["file"] = name
            assets_state[key] = merged
            if old_key != key:
                assets_state.pop(old_key, None)
            continue

        version = ""
        if archive:
            version = sanitize_name(item.get("version")) or ("sha-" + sha[:8])
            target_name = "%s-%s%s" % (stem, version, ext)
            target = os.path.join(work_dir, target_name)
            if os.path.exists(target):
                version = "%s-%s" % (version, time.strftime("%Y%m%d%H%M%S"))
                target_name = "%s-%s%s" % (stem, version, ext)
                target = os.path.join(work_dir, target_name)
        else:
            target_name = name
            target = os.path.join(work_dir, target_name)
        os.replace(part, target)
        log("saved %s (%d bytes, sha256 %s)" % (
            os.path.join(item.get("subdir") or "", target_name) if item.get("subdir")
            else target_name, size, sha[:12]))

        if not archive:
            removed = remove_versioned_files(cfg, work_dir, name)
            if removed:
                log("removed old versions: %s" % ", ".join(removed))

        if archive:
            stable = cfg.stable_for(name, matched_total)
            if stable:
                if cfg.stable_name and stable != cfg.stable_name and not stable_warned:
                    log("STABLE_NAME ignored: %d assets matched, keeping original filenames"
                        % matched_total)
                    stable_warned = True
                stable_path = os.path.join(work_dir, stable)
                temp = stable_path + ".tmp"
                shutil.copyfile(target, temp)
                os.replace(temp, stable_path)
                log("updated %s" % (os.path.join(item.get("subdir") or "", stable)
                                    if item.get("subdir") else stable))

            removed = prune_old(cfg, work_dir, stem, ext)
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
        jobs = [("raw", lambda: plan_raw(cfg, state), None)]
    else:
        jobs = [("release:%s" % target.label,
                 (lambda target=target: plan_release(cfg, state, target)), target)
                for target in cfg.targets]
    errors = []
    for label, planner, target in jobs:
        try:
            plan = planner()
            if plan.get("tag"):
                state.setdefault("tags", {})[label] = plan["tag"]
            if plan["action"] == "none":
                if target is not None and not target.archive:
                    removed = cleanup_target_versioned(cfg, state, target)
                    if removed:
                        log("removed old versions: %s" % ", ".join(removed))
                log("no update: %s" % plan.get("reason"))
            else:
                log("update found: %s" % plan.get("description"))
                apply_download(cfg, state, plan)
                if plan.get("release_url"):
                    state["release_url"] = plan["release_url"]
            if target is not None and plan.get("asset_names"):
                removed = cleanup_stale_names(cfg, state, target, target.repo or cfg.repo,
                                              plan["asset_names"])
                if removed:
                    log("removed old versions (renamed): %s" % ", ".join(removed))
        except Exception as error:
            errors.append("%s: %s: %s" % (label, type(error).__name__, error))
    if errors:
        raise RuntimeError("; ".join(errors))


def validate(cfg):
    if cfg.mode not in ("release", "raw"):
        return "invalid MODE=%s (use release or raw)" % cfg.mode
    if cfg.mode == "release" and not cfg.repo:
        return "release mode needs GITHUB_REPO=owner/repo"
    if cfg.mode == "raw" and not (cfg.raw_url or (cfg.repo and cfg.raw_path)):
        return "raw mode needs RAW_URL or GITHUB_REPO + RAW_PATH"
    return ""


def validate_config_update(cfg, data):
    download_dir = str(data.get("download_dir") or cfg.download_dir).strip()
    if not download_dir.startswith("/"):
        return "保存位置必须是绝对路径"
    if not (download_dir == "/nas" or download_dir.startswith("/nas/")
            or download_dir == "/download" or download_dir.startswith("/download/")):
        return "保存位置需位于 /nas 下（/nas = compose 中 NAS_ROOT 挂载的 NAS 磁盘）"
    repo = str(data.get("repo") or cfg.repo or "").strip()
    if not repo or "/" not in repo:
        return "仓库格式应为 owner/repo"
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        return "至少需要一个下载目标"
    for item in targets:
        if not isinstance(item, dict):
            return "下载目标格式错误"
        patterns = item.get("patterns")
        if isinstance(patterns, str):
            patterns = [part for part in patterns.split(",") if part.strip()]
        if not [part for part in (patterns or []) if str(part).strip()]:
            return "每个下载目标的匹配规则不能为空"
        target_repo = str(item.get("repo") or "").strip().strip("/")
        if target_repo and "/" not in target_repo:
            return "目标仓库格式应为 owner/repo（或留空使用默认仓库）"
        subdir = str(item.get("subdir") or "").strip()
        if subdir and ("/" in subdir or "\\" in subdir or subdir in (".", "..")
                       or subdir.startswith(".")):
            return "子文件夹名不能包含 / 且不能以 . 开头"
        cleanup = item.get("cleanup")
        if isinstance(cleanup, str):
            cleanup = [part for part in cleanup.split(",") if part.strip()]
        for entry in cleanup or []:
            text = str(entry).strip()
            if not text:
                continue
            if "/" in text or "\\" in text:
                return "旧版本清理模板不能包含 /（例如 runtime-mpv-*-arm64-v8a.apk）"
    raw_days = data.get("check_interval_days")
    if raw_days is None:
        raw_days = data.get("check_interval_seconds")
        if raw_days is not None:
            try:
                seconds = max(0, int(raw_days))
                raw_days = 0 if seconds <= 0 else max(1, int(round(seconds / 86400.0)))
            except (TypeError, ValueError):
                return "检查间隔必须是数字"
    if raw_days is None:
        raw_days = cfg.interval_days
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        return "检查间隔(天)必须是整数"
    if days < 0:
        return "检查间隔(天)不能为负数（0 = 不自动检查）"
    raw_hour = data.get("check_hour")
    if raw_hour not in (None, ""):
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            return "更新时间需为 0-23 的整数（留空 = 不指定）"
        if not 0 <= hour <= 23:
            return "更新时间需为 0-23 的整数（留空 = 不指定）"
    return ""


def clean_config_payload(cfg, data):
    raw_days = data.get("check_interval_days")
    if raw_days is None:
        raw_days = data.get("check_interval_seconds")
        if raw_days is not None:
            seconds = max(0, int(raw_days))
            raw_days = 0 if seconds <= 0 else max(1, int(round(seconds / 86400.0)))
    days = cfg.interval_days if raw_days is None else max(0, int(raw_days))
    if "check_hour" in data:
        hour = parse_hour(data.get("check_hour"))
    else:
        hour = cfg.check_hour
    cleaned = {
        "repo": str(data.get("repo") or cfg.repo).strip().strip("/"),
        "download_dir": str(data.get("download_dir") or cfg.download_dir).strip().rstrip("/") or "/",
        "check_interval_days": days,
        "check_hour": "" if hour is None else hour,
        "keep_versions": int(data.get("keep_versions") if data.get("keep_versions") is not None
                             else cfg.keep_versions),
        "stable_name": str(data.get("stable_name") if data.get("stable_name") is not None else "auto"),
        "proxy_prefix": str(data.get("proxy_prefix") or "").strip(),
        "github_token": str(data.get("github_token") or "").strip(),
        "include_prerelease": bool(data.get("include_prerelease")),
        "targets": [],
        "mode": cfg.mode,
    }
    for item in data.get("targets") or []:
        patterns = item.get("patterns")
        if isinstance(patterns, str):
            patterns = [part.strip() for part in patterns.split(",")]
        patterns = [str(part).strip() for part in (patterns or []) if str(part).strip()]
        cleanup = item.get("cleanup")
        if isinstance(cleanup, str):
            cleanup = [part.strip() for part in cleanup.split(",")]
        cleanup = [str(part).strip() for part in (cleanup or []) if str(part).strip()]
        cleaned["targets"].append({
            "repo": str(item.get("repo") or "").strip().strip("/"),
            "tag": str(item.get("tag") or "").strip(),
            "patterns": patterns or ["*.apk"],
            "archive": bool(item.get("archive", True)),
            "subdir": str(item.get("subdir") or "").strip(),
            "cleanup": cleanup,
        })
    return cleaned


def is_new_state_key(key):
    return key.count(":") >= 2 and "/" in key.split(":", 1)[0]


def migrate_legacy(cfg, state):
    if cfg.mode != "release":
        return False
    assets_state = state.get("assets") or {}
    claims = {}
    for key, entry in list(assets_state.items()):
        if key.startswith("raw:") or not entry.get("asset_id"):
            continue
        if is_new_state_key(key):
            continue
        name = key.rsplit(":", 1)[-1] if ":" in key else key
        for target in cfg.targets:
            if any(fnmatch.fnmatch(name.lower(), pattern.lower())
                   for pattern in target.patterns):
                claims[key] = (target, name)
                break
    changed = False
    for key, (target, name) in claims.items():
        entry = assets_state.get(key)
        if entry is None:
            continue
        subdir = cfg.target_subdir(target)
        dest_dir = os.path.join(cfg.download_dir, subdir) if subdir else cfg.download_dir
        names = []
        if entry.get("file"):
            names.append(entry["file"])
        if name not in names:
            names.append(name)
        moved = False
        for file_name in names:
            src = os.path.join(cfg.download_dir, file_name)
            dest = os.path.join(dest_dir, file_name)
            if os.path.isfile(src) and not os.path.exists(dest):
                os.makedirs(dest_dir, exist_ok=True)
                try:
                    os.replace(src, dest)
                    log("moved %s -> %s/" % (file_name, subdir or "."))
                    moved = True
                except OSError as error:
                    log("move failed %s: %s" % (file_name, error))
        new_key = "%s:%s:%s" % (target.repo, entry.get("tag") or target.tag or "latest", name)
        if new_key != key and new_key not in assets_state:
            assets_state[new_key] = entry
            assets_state.pop(key, None)
            changed = True
        elif moved:
            changed = True
    return changed


class Updater:
    def __init__(self):
        self.lock = threading.RLock()
        self.trigger = threading.Event()
        self.stop = threading.Event()
        self.next_deadline = None
        self.config_path = env("CONFIG_FILE", "/data/config.json")
        self.state_path = env("STATE_FILE", "/data/state.json")
        data = self.load_config()
        self.cfg = Config(data)
        self.state = load_json(self.state_path) or {}
        if migrate_legacy(self.cfg, self.state):
            save_json(self.state_path, self.state)

    def load_config(self):
        data = load_json(self.config_path)
        if data is None:
            data = Config().to_config_dict()
            save_json(self.config_path, data)
            log("created %s from environment defaults" % self.config_path)
        return data

    def save_config(self, payload):
        with self.lock:
            problem = validate_config_update(self.cfg, payload)
            if problem:
                raise ValueError(problem)
            cleaned = clean_config_payload(self.cfg, payload)
            save_json(self.config_path, cleaned)
            self.cfg = Config(cleaned)
        log("config updated via panel (dir=%s, targets=%d, schedule=%s)"
            % (self.cfg.download_dir, len(self.cfg.targets), self.cfg.schedule_desc()))
        try:
            os.makedirs(self.cfg.download_dir, exist_ok=True)
        except OSError as error:
            log("warning: cannot create %s: %s" % (self.cfg.download_dir, error))
        if self.cfg.interval_days > 0:
            self.trigger.set()
        return self.cfg

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.cfg), copy.deepcopy(self.state)

    def run_forever(self):
        first = True
        while not self.stop.is_set():
            cfg, _ = self.snapshot()
            skip_first = first and not cfg.run_once \
                and (cfg.interval_days <= 0 or not cfg.run_on_start)
            if first and not cfg.run_once:
                if cfg.interval_days <= 0:
                    log("自动检查已关闭（检查间隔=0天），仅手动检查生效")
                elif not cfg.run_on_start:
                    log("RUN_ON_START=0, waiting for next scheduled check (%s)" % cfg.schedule_desc())
            if not skip_first:
                local_state = self.snapshot()[1]
                try:
                    check_once(cfg, local_state)
                except Exception as error:
                    log("check failed: %s: %s" % (type(error).__name__, error))
                with self.lock:
                    local_state["last_check_ts"] = time.time()
                    local_state["last_check_iso"] = now_iso()
                    self.state = local_state
                    save_json(self.state_path, self.state)
                if cfg.run_once:
                    log("RUN_ONCE=1, exiting")
                    self.stop.set()
                    return
            first = False
            deadline = self.next_check_time(cfg)
            with self.lock:
                self.next_deadline = deadline
            self.wait_next(deadline)

    def next_check_time(self, cfg, base=None):
        if cfg.interval_days <= 0:
            return None
        base = time.time() if base is None else base
        step = cfg.interval_days * 86400
        if cfg.check_hour is None:
            return base + step
        local = time.localtime(base)
        target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                              cfg.check_hour, 0, 0, 0, 0, -1))
        while target <= base + 1:
            target += step
        return target

    def wait_next(self, deadline):
        if deadline is None:
            while not self.stop.is_set():
                if self.trigger.wait(timeout=1):
                    self.trigger.clear()
                    return
            return
        while not self.stop.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            if self.trigger.wait(timeout=min(remaining, 5)):
                self.trigger.clear()
                return

    def status(self):
        cfg, state = self.snapshot()
        files = []

        def collect(directory, subdir):
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
            except OSError:
                return
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_file():
                    info = entry.stat()
                    files.append({"name": entry.name, "dir": subdir, "size": info.st_size,
                                  "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                         time.localtime(info.st_mtime))})
                elif entry.is_dir():
                    collect(entry.path, entry.name)

        collect(cfg.download_dir, "")
        last = float(state.get("last_check_ts") or 0)
        tags = state.get("tags") or {}
        main_tag = None
        if cfg.mode == "release" and cfg.targets:
            main_tag = tags.get("release:%s" % cfg.targets[0].label)
        if not main_tag:
            main_tag = state.get("tag")
        if cfg.interval_days > 0:
            deadline = getattr(self, "next_deadline", None) or self.next_check_time(cfg)
            next_seconds = max(0, int(deadline - time.time()))
        else:
            next_seconds = None
        result = {
            "ok": True,
            "mode": cfg.mode,
            "config": cfg.to_config_dict(),
            "target_desc": cfg.target_desc(),
            "last_check_iso": state.get("last_check_iso"),
            "next_check_seconds": next_seconds,
            "auto_check_enabled": cfg.interval_days > 0,
            "tag": main_tag,
            "tags": tags,
            "assets": state.get("assets") or {},
            "files": files,
            "logs": list(LOG_LINES)[-300:],
            "panel_token_required": bool(PANEL_TOKEN),
        }
        return result

    def health(self):
        cfg, state = self.snapshot()
        last = float(state.get("last_check_ts") or 0)
        if last and cfg.interval_days > 0:
            window = max(2 * cfg.interval_days * 86400, 3600)
            healthy = (time.time() - last) <= window
        else:
            healthy = True
        return healthy, {"ok": True, "healthy": healthy, "last_check_ts": last}


PANEL_TOKEN = env("PANEL_TOKEN")


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "apk-updater-panel/1.0"
    updater = None

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not PANEL_TOKEN:
            return True
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("token", [""])[0] == PANEL_TOKEN:
            return True
        return self.headers.get("X-Panel-Token", "") == PANEL_TOKEN

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            return None

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/panel"):
            try:
                with open(PANEL_FILE, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.send_json({"error": "panel.html missing"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/healthz":
            healthy, payload = self.updater.health()
            self.send_json(payload, 200 if healthy else 503)
            return
        if path == "/status":
            if not self.authorized():
                self.send_json({"error": "unauthorized"}, 401)
                return
            self.send_json(self.updater.status())
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.authorized():
            self.send_json({"error": "unauthorized"}, 401)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/config":
            payload = self.read_body()
            if payload is None:
                self.send_json({"error": "invalid json"}, 400)
                return
            try:
                cfg = self.updater.save_config(payload)
            except ValueError as error:
                self.send_json({"error": str(error)}, 400)
                return
            self.send_json({"ok": True, "config": cfg.to_config_dict()})
            return
        if path == "/api/check":
            self.updater.trigger.set()
            log("manual check requested via panel")
            self.send_json({"ok": True, "message": "已触发检查"})
            return
        self.send_json({"error": "not found"}, 404)


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


def main():
    setup_stdout()
    updater = Updater()
    problem = validate(updater.cfg)
    if problem:
        log("config error: %s" % problem)
        return 2
    try:
        os.makedirs(updater.cfg.download_dir, exist_ok=True)
    except OSError as error:
        log("warning: cannot create %s: %s" % (updater.cfg.download_dir, error))
    port = env_int("PORT", 8080)
    handler = PanelHandler
    handler.updater = updater
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("apk-updater start | mode=%s dir=%s schedule=%s keep=%d" % (
        updater.cfg.mode, updater.cfg.download_dir, updater.cfg.schedule_desc(),
        updater.cfg.keep_versions))
    log("target: %s" % updater.cfg.target_desc())
    log("panel: http://<nas-ip>:%d/ | token %s"
        % (port, "enabled" if PANEL_TOKEN else "disabled"))
    if updater.cfg.token:
        log("github token: configured")
    if updater.cfg.proxy_prefixes:
        log("proxy prefixes: %s" % ", ".join(updater.cfg.proxy_prefixes))
    install_signal_handlers()
    updater.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
