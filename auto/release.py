#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把镜像包与安装包上传到 GitHub / Gitee Releases（由 sh auto/release.sh 调用）"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_BODY = """\
## 使用方法

1. 下载附件 **{image_name}**，在 NAS 上导入镜像：

   ```sh
   docker load -i {image_name}
   ```

2. 下载附件 **{install_name}** 并解压（内含 docker-compose.yml、.env.example）：

   ```sh
   tar -xzf {install_name} -C apk-updater
   cd apk-updater
   cp .env.example .env   # 按需修改 NAS_ROOT / GITHUB_REPO / DOWNLOAD_DIR 等
   docker compose up -d
   ```

3. 浏览器打开面板：`http://<NAS地址>:8898/`

详细说明见附件 README.md 或仓库主页。
"""


def log(message):
    print(message, flush=True)


def remote_slug(remote):
    try:
        result = subprocess.run(["git", "remote", "get-url", remote],
                                capture_output=True, encoding="utf-8", errors="replace")
    except OSError:
        return ""
    url = (result.stdout or "").strip()
    if not url:
        return ""
    match = re.search(r"[:/]([\w.\-]+/[\w.\-]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else ""


def request(method, url, headers=None, data=None, timeout=120, ok=(200,)):
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        payload = error.read()
    if status not in ok:
        text = payload.decode("utf-8", "replace")[:400]
        raise RuntimeError("%s %s -> HTTP %s: %s" % (method, url, status, text))
    return status, payload


def request_json(method, url, headers=None, data=None, ok=(200,)):
    status, payload = request(method, url, headers, data, ok=ok)
    if not payload:
        return status, {}
    parsed = json.loads(payload.decode("utf-8", "replace"))
    return status, parsed if isinstance(parsed, dict) else {}


def build_multipart(fields, file_field, filename, content):
    boundary = "----apkupd" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, key, value)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n"
                  % (boundary, file_field, filename)).encode("utf-8"))
    parts.append(content)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def upload_github(slug, tag, body, token, paths):
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/vnd.github+json",
               "User-Agent": "apk-updater-release"}
    api = "https://api.github.com/repos/" + slug
    status, release = request_json("GET", api + "/releases/tags/" + urllib.parse.quote(tag),
                                   headers, ok=(200, 404))
    if status == 404:
        payload = json.dumps({"tag_name": tag, "name": tag, "body": body,
                              "target_commitish": "main"}).encode("utf-8")
        _, release = request_json("POST", api + "/releases", headers, payload, ok=(201,))
        log("  github: created release %s" % tag)
    else:
        log("  github: using existing release %s" % tag)
    release_id = release["id"]
    existing = {}
    for asset in release.get("assets", []):
        existing[asset.get("name")] = asset.get("id")
    for path in paths:
        name = os.path.basename(path)
        if name in existing:
            request("DELETE", "%s/releases/assets/%s" % (api, existing[name]),
                    headers, ok=(204, 200))
            log("  github: removed old asset %s" % name)
        with open(path, "rb") as handle:
            data = handle.read()
        log("  github: uploading %s (%.1f MB)..." % (name, len(data) / 1048576.0))
        url = ("https://uploads.github.com/repos/%s/releases/%d/assets?name=%s"
               % (slug, release_id, urllib.parse.quote(name)))
        upload_headers = dict(headers)
        upload_headers["Content-Type"] = "application/octet-stream"
        request("POST", url, upload_headers, data, timeout=3600, ok=(201,))
    return "https://github.com/%s/releases/tag/%s" % (slug, tag)


def upload_gitee(slug, tag, body, token, paths):
    base = "https://gitee.com/api/v5/repos/" + slug
    status, release = request_json(
        "GET", "%s/releases/tags/%s?access_token=%s"
        % (base, urllib.parse.quote(tag), urllib.parse.quote(token)), ok=(200, 404))
    if not release.get("id"):
        fields = {"access_token": token, "tag_name": tag, "name": tag, "body": body,
                  "target_commitish": "main"}
        data = urllib.parse.urlencode(fields).encode("utf-8")
        _, release = request_json("POST", base + "/releases",
                                  {"Content-Type": "application/x-www-form-urlencoded"},
                                  data, ok=(200, 201))
        if not release.get("id"):
            raise RuntimeError("gitee create release failed: %s" % str(release)[:300])
        log("  gitee: created release %s" % tag)
    else:
        log("  gitee: using existing release %s" % tag)
    release_id = release["id"]
    for path in paths:
        name = os.path.basename(path)
        with open(path, "rb") as handle:
            content = handle.read()
        log("  gitee: uploading %s (%.1f MB)..." % (name, len(content) / 1048576.0))
        data, ctype = build_multipart({"access_token": token}, "file", name, content)
        url = "%s/releases/%s/attach_files" % (base, release_id)
        try:
            request("POST", url, {"Content-Type": ctype}, data, timeout=3600, ok=(200, 201))
        except RuntimeError as error:
            log("  gitee: upload failed (可能已存在同名附件，可先在页面上删除): %s" % error)
    return "https://gitee.com/%s/releases/tag/%s" % (slug, tag)


def main():
    parser = argparse.ArgumentParser(description="upload image bundle to GitHub/Gitee releases")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v2026.10.27")
    parser.add_argument("--dir", default="dist", help="directory with packaged files")
    parser.add_argument("--github", default="", help="owner/repo (default: origin remote)")
    parser.add_argument("--gitee", default="", help="owner/repo (default: gitee remote)")
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-gitee", action="store_true")
    args = parser.parse_args()

    dist = os.path.abspath(args.dir)
    paths = sorted(glob.glob(os.path.join(dist, "*.tar.gz")))
    readme = os.path.abspath("README.md")
    if os.path.isfile(readme):
        paths.append(readme)
    if not paths:
        log("no files to upload in %s" % dist)
        return 1

    def pick(suffix, fallback):
        for path in paths:
            if os.path.basename(path).endswith(suffix):
                return os.path.basename(path)
        return fallback

    image_name = pick("-image.tar.gz", "image.tar.gz")
    install_name = pick("-install.tar.gz", "install.tar.gz")
    body = DEFAULT_BODY.format(image_name=image_name, install_name=install_name)

    github_slug = args.github.strip().strip("/") or remote_slug("origin")
    gitee_slug = args.gitee.strip().strip("/") or remote_slug("gitee")
    result = 0

    if not args.skip_github:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            log("GITHUB_TOKEN not set (.env), skip github")
            result = 1
        elif not github_slug:
            log("github slug unknown, skip github (use --github owner/repo)")
            result = 1
        else:
            log("github: %s @ %s" % (github_slug, args.tag))
            try:
                log("  github release: %s" % upload_github(github_slug, args.tag, body, token, paths))
            except Exception as error:
                log("  github upload failed: %s" % error)
                result = 1

    if not args.skip_gitee:
        token = os.environ.get("GITEE_TOKEN", "").strip()
        if not token:
            log("GITEE_TOKEN not set (.env), skip gitee")
            result = 1
        elif not gitee_slug:
            log("gitee slug unknown, skip gitee (use --gitee owner/repo)")
            result = 1
        else:
            log("gitee: %s @ %s" % (gitee_slug, args.tag))
            try:
                log("  gitee release: %s" % upload_gitee(gitee_slug, args.tag, body, token, paths))
            except Exception as error:
                log("  gitee upload failed: %s" % error)
                result = 1

    return result


if __name__ == "__main__":
    sys.exit(main())
