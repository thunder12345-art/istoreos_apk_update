# istoreos_apk_update（apk-updater）

NAS（iStoreOS）上的 **APK 自动更新助手**：监控 GitHub Releases（支持多仓库、多目标），自动下载 APK 到 NAS 指定目录并清理旧版本，自带中文 Web 控制面板。

- GitHub: https://github.com/thunder12345-art/istoreos_apk_update
- Gitee:  https://gitee.com/thunder0629/istoreos_apk_update

## 功能特性

- 多仓库 / 多目标：如 FongMi/Release 的 `最新版` 与 `tag=module`，以及第三方仓库 `pass-with-high-score/universal-installer`
- 每个目标独立子文件夹（默认「仓库-tag」，如 `FongMi-Release`、`FongMi-Release-module`，可在面板自定义）
- 两种保存方式：版本化保存（保留 N 份 + 固定名副本）或按原始文件名保存
- 旧版本自动清理：自动识别文件名中的版本号位置（`1.0.7 → 1.0.9`），也可用 `*` 模板精确指定（如 `runtime-mpv-*-arm64-v8a.apk`）
- 定时检查：每 N 天 + 指定时间点（如每天 10 点），也可关闭自动检查仅手动
- Web 面板：改配置即时生效、手动触发检查、查看文件列表/日志/倒计时
- 下载代理前缀（gh-proxy.com 等）、sha256 校验、失败自动切换下载源
- 双推送：代码同步 GitHub + Gitee，镜像包发布到 Releases

## 快速开始（方式一：下载发布包，推荐）

1. 在 Releases 页面下载镜像包 `istoreos-apk-update-<版本>-image.tar.gz`
2. 在 NAS 上导入镜像：
   ```sh
   docker load -i istoreos-apk-update-<版本>-image.tar.gz
   ```
3. 下载安装包 `istoreos-apk-update-<版本>-install.tar.gz` 并解压（内含 `docker-compose.yml`、`.env.example`）：
   ```sh
   tar -xzf istoreos-apk-update-<版本>-install.tar.gz -C apk-updater
   cd apk-updater
   ```
4. 生成配置并按需修改（至少确认 `NAS_ROOT`、`GITHUB_REPO`、`DOWNLOAD_DIR`）：
   ```sh
   cp .env.example .env
   vi .env
   ```
   关键项示例：
   ```ini
   NAS_ROOT=/mnt/data_sda1        # NAS 数据盘根目录（映射为容器 /nas）
   DOWNLOAD_DIR=/nas/TV           # APK 保存位置
   GITHUB_REPO=FongMi/Release     # 监控仓库
   ASSET_PATTERN=leanback-armeabi_v7a.apk,mobile-arm64_v8a.apk
   EXTRA_TARGETS=module|*.apk|0;pass-with-high-score/universal-installer||*.apk|1
   PROXY_PREFIX=https://gh-proxy.com/
   CHECK_INTERVAL_DAYS=1
   CHECK_HOUR=10                  # 每天 10 点检查（留空 = 按间隔执行）
   ```
5. 启动：
   ```sh
   docker compose up -d
   ```
6. 浏览器打开面板：`http://<NAS地址>:8898/`

## 快速开始（方式二：源码构建）

```sh
git clone https://github.com/thunder12345-art/istoreos_apk_update.git
cd istoreos_apk_update
cp .env.example .env
vi .env
docker compose up -d --build
```

## 面板使用

- **基本配置**：默认仓库、保存位置、检查间隔（天，0 = 不自动检查）、更新时间（0-23 时）、保留版本数、固定名（auto = 原始文件名）、代理前缀、GitHub 令牌、预发布开关
- **下载目标**：每行一个目标，字段说明：

  | 列 | 说明 |
  | --- | --- |
  | 仓库 | 留空 = 使用默认仓库；跨仓库填 `owner/repo` |
  | tag | 留空 = 最新版（自动跳过预发布）；填 `module` 等则监控对应 tag |
  | 文件匹配 | 逗号分隔，如 `*.apk`；用于从 Release 中挑选附件 |
  | 版本化 | 勾选 = 保存为 `名称-版本.apk` + 固定名副本，按保留数清理；不勾选 = 按原始文件名保存 |
  | 子文件夹 | 留空自动 = 「仓库-tag」；可自定义（如 `TV`） |
  | 旧版本清理模板 | 留空 = 自动识别版本号（`1.0.7→1.0.9` 自动删旧）；也可指定 `*` 版本位，如 `runtime-mpv-*-arm64-v8a.apk`（逗号分隔多条，一条内可多个 `*`；填写后该目标完全按模板清理） |

- 「保存配置」即时生效并触发一次检查；「立即检查」手动触发；文件列表与日志实时刷新

## 配置项（.env ↔ 面板）

| .env | 面板字段 | 说明 |
| --- | --- | --- |
| `PANEL_PORT` | - | 面板端口（默认 8898） |
| `PANEL_TOKEN` | - | 面板访问口令，留空 = 局域网免密 |
| `NAS_ROOT` | - | NAS 数据盘根目录，映射为容器 `/nas` |
| `DOWNLOAD_DIR` | 保存位置 | APK 保存目录（需在 `/nas` 下） |
| `GITHUB_REPO` | 默认仓库 | `owner/repo` |
| `ASSET_PATTERN` | 主目标匹配 | 默认仓库的文件匹配规则 |
| `EXTRA_TARGETS` | 其他目标行 | 见下方格式说明 |
| `PROXY_PREFIX` | 代理前缀 | 如 `https://gh-proxy.com/`，多个逗号分隔 |
| `STABLE_NAME` | 固定名 | `auto` = 固定名副本用原始文件名 |
| `KEEP_VERSIONS` | 保留版本数 | 版本化保存时保留的新旧份数 |
| `CHECK_INTERVAL_DAYS` | 检查间隔（天） | `0` = 不自动检查 |
| `CHECK_HOUR` | 更新时间 | `0-23`，如 `10` = 每天 10 点；留空 = 从上次检查起算间隔 |
| `GITHUB_TOKEN` | GitHub 令牌 | 可选：提升 API 限额/私有仓库 |
| `MODE` / `RAW_*` | - | raw 模式（监控单个文件），一般不用 |

`EXTRA_TARGETS` 格式（分号分隔多个目标）：

```
1) tag|匹配规则|archive                      （默认仓库，如 module 目标）
2) 仓库|tag|匹配规则|archive                 （跨仓库，仓库留空 = 默认仓库）
3) 仓库|tag|匹配规则|archive|旧版本清理模板  （第 5 段可选，示例见上）
```

## 维护者：更新与发布

1. 本机生成 bundle（`git bundle create apk-updater.bundle main`）→ 拷到 NAS `/mnt/sata1-2/docker/`
2. NAS 上执行：
   ```sh
   cd /mnt/sata1-2/docker/apk-updater && sh auto/nas_update.sh
   ```
   自动：停止容器 → 合并 bundle → 提交 → 推送 GitHub + Gitee → 重建镜像 → 启动容器
3. 在 NAS 上发布新版本（构建镜像包并上传 Releases）：
   ```sh
   sh auto/release.sh v2026.10.27
   ```
   需要 `.env` 中配置：`GITHUB_TOKEN`（repo 权限）、`GITEE_TOKEN`（projects 权限）

   NAS 首次需配置远端（一次性）：
   ```sh
   git remote add origin git@github.com:thunder12345-art/istoreos_apk_update.git
   git remote add gitee  https://gitee.com/thunder0629/istoreos_apk_update.git
   ```

## 常见问题

- **面板打不开**：`docker compose ps` 看容器状态，`docker logs --tail 30 apk-updater` 看日志
- **取到旧版本**：本工具对 `latest` 采用按发布时间排序（GitHub `/releases/latest` 对部分仓库不可靠，已规避）
- **磁盘空间不足（ENOSPC）**：确认 `NAS_ROOT` 指向真实挂载点（如 `/mnt/data_sda1`），`/mnt/sata1-1` 之类可能是 tmpfs 占位，并未挂载
- **下载慢/失败**：设置 `PROXY_PREFIX=https://gh-proxy.com/`（多个代理可逗号分隔，自动回退）
- **数据文件**：容器 `./data/config.json`（面板配置，首次由 .env 生成后以此为准）、`./data/state.json`（下载状态）、`./data/updater.log`（日志）
