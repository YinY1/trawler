# Server Image Registry + Auto-Update Plan

**Date**: 2026-06-25
**Status**: Draft / Pending Review
**Author**: @oracle (architecture)
**Scope**: Build/deploy infra — no application code changes

---

## 0. Goal

替换当前「SSH 上服务器 → git clone → docker build」的手工流程，改为：

1. 本地（或 CI）push 代码 → 镜像自动构建并推到 registry
2. 服务器定期检查新镜像 → 自动拉取并重启容器
3. 失败可回滚，PAT/凭据不落盘明文

**非目标**：不改应用代码、不改 web 入口（Caddy/Cloudflare 链路保持不变）、不引入 k8s/-swarm。

---

## 1. Current State Recap

| 维度 | 现状 |
|---|---|
| 服务器 | SSH alias `usa` → `43.162.107.208`, 部署路径 `/root/app/trawler` |
| 镜像 | `trawler:local`，服务器本地 `docker build` |
| Compose | `/root/app/docker-compose.yml`（caddy + gotify + relay + trawler） |
| 端口 | `127.0.0.1:8080`, healthcheck `curl /login` |
| Web | `https://trawl.priestress.host`（Cloudflare → Caddy → 8080） |
| Dockerfile | Python 3.14-slim, ffmpeg+supervisor+cron+tini, 单容器托管 web+cron |
| 架构 | **待确认** — 见 §10 Task 0。腾讯云 43.162.x 多为 amd64，但必须实测 |

更新痛点：每次发布需 SSH、`git clone`、`cp config`、`docker build`（含 whisper 模型预热可能 10+ 分钟）、`compose up`。无版本号、无回滚、服务器需装 git + 源码。

---

## 2. 方案对比

### 方案 A — 自建 Private Registry（`registry:2` + Caddy TLS + basic auth）

**架构**：
```
GitHub ──(本地 push)──▶ usa:5000 (registry:2)
                              ▲
            Caddy (TLS, basic auth) 反代
                              │
       Watchtower / cron ◀── pull
```

| 维度 | 评价 |
|---|---|
| 复杂度 | **高**。需自管 registry 容器、TLS 证书（Cloudflare Origin Cert 或 Caddy ACME）、htpasswd basic auth、存储后端（filesystem bind mount）、GC 策略 |
| 维护成本 | **高**。磁盘满（旧 tag 不清理）、证书过期、registry 挂了影响所有更新 |
| 安全性 | 中。basic auth 弱，registry 暴露公网风险（即使 Cloudflare 前置也得开端口）|
| 是否真自动 | 半自动。仍需本地/CI push，且服务器自己就是 registry（自吃自） |
| 外部依赖 | 无（这是唯一优点） |
| 成本 | 服务器磁盘 + 带宽（每次 pull 走本地回路可忽略；但构建仍占服务器资源） |

**结论**：**不推荐**。单服务器场景下「服务器自己当 registry」绕了一圈，没有解决「不在服务器 build」的核心诉求。只有在多服务器 + 私有内网场景才有意义。

---

### 方案 B — GHCR (GitHub Container Registry) + GitHub Actions 构建

**架构**：
```
git push master ──▶ GitHub Actions ──▶ build (amd64) ──▶ push ghcr.io/yiny1/trawler
                                                                    │
                                                       usa: docker login + pull
                                                                    │
                                                         Watchtower / cron
```

| 维度 | 评价 |
|---|---|
| 复杂度 | **低**。Actions workflow 一个文件，GHCR 免费、零运维 |
| 维护成本 | **极低**。GitHub 管存储、GC、TLS、CDN |
| 安全性 | **高**。PAT 走 GitHub 正规渠道，可细粒度（仅 `read:packages`），随时撤销 |
| 是否真自动 | ✅ push 触发构建 + 服务器 watchtower/cron 自动拉 |
| 外部依赖 | GitHub（repo 已在那，不算新增依赖） |
| 成本 | **免费额度内**：Actions 私有仓库 2000 min/月（一次 build ~5-8 min），GHCR 私有 500MB + 1GB/月流量免费；公开仓库完全免费 |

**关键点**：repo 已在 `github.com/YinY1/trawler`，GHCR 是「原生路径」，零额外账号。

**结论**：**强推荐**。详见 §3。

---

### 方案 C — Watchtower + 自建 Registry

= 方案 A 的 registry + 方案 B 的 watchtower 自动拉。

**结论**：**不推荐**。Watchtower 本身是好东西（方案 B 也要用），但叠加自建 registry 把简单事复杂化。Watchtower 的价值在于「自动拉」，registry 自建与否是正交问题 — 应该解耦：GHCR（存储）+ Watchtower（自动拉）。

---

### 方案 D — rsync image tar (`docker save` → `rsync` → `docker load`)

**流程**：
```
本地 build ──▶ docker save ──tar──▶ rsync usa:/tmp/ ──▶ docker load ──▶ compose up
```

| 维度 | 评价 |
|---|---|
| 复杂度 | 中。需写 save/load 脚本，无版本管理 |
| 维护成本 | 中。每次 ~1-2GB tar 传输，无 layer 复用（每次全量） |
| 安全性 | 中。走 SSH，但无 registry 的鉴权粒度 |
| 是否真自动 | ❌ 需人工触发 rsync |
| 外部依赖 | 无 |
| 成本 | 带宽高、慢 |

**结论**：**不推荐**。无 registry 的好处（无外部依赖）但比 GHCR 更麻烦。仅适合「完全气隙环境」。

---

### 总成本对比

| 方案 | 一次性成本 | 月运营成本 | 构建时长/次 | 网络流量/次 |
|---|---|---|---|---|
| A 自建 registry | 高（4-6h 搭建） | 中（磁盘+证书） | 服务器本地 ~10min | 0（本地回路） |
| **B GHCR** | **低（1-2h）** | **0** | **CI ~6min** | **~50-200MB（layer cache 后）** |
| C A+B | 高 | 中 | CI ~6min | ~50-200MB |
| D rsync | 中（2h） | 低 | 本地 ~10min + rsync ~3min | 1-2GB 全量 |

---

## 3. 推荐方案：B (GHCR) + Watchtower

**理由**：

1. **匹配现实**：单人、单服务器、repo 已在 GitHub — GHCR 是零摩擦选择
2. **解决核心痛点**：服务器不再需要 git clone + docker build，省磁盘/CPU/时间
3. **零运维**：GitHub 管 registry 全部运维，省下的精力用在做产品
4. **安全可控**：PAT 可细粒度、可撤销；私有镜像只有持 token 能拉
5. **Watchtower 提供自动化最后一公里**：检测新 tag → 拉取 → 重启 → 清旧

**架构图**：
```
┌──────────────┐   push master    ┌──────────────────┐
│  Developer   │ ───────────────▶ │   GitHub Repo    │
│  (本地)      │                  │  YinY1/trawler   │
└──────────────┘                  └────────┬─────────┘
                                           │ trigger
                                           ▼
                                  ┌──────────────────┐
                                  │ GitHub Actions   │
                                  │ build & push     │
                                  └────────┬─────────┘
                                           │ push image
                                           ▼
                                  ┌──────────────────┐
                                  │  ghcr.io/yiny1/  │
                                  │    trawler       │  ◀─── 服务器 docker login (PAT, read:packages)
                                  └────────┬─────────┘
                                           │ poll every 5min
                                           ▼
                                  ┌──────────────────┐
                                  │ usa: Watchtower  │
                                  │ container        │
                                  └────────┬─────────┘
                                           │ new image detected
                                           ▼
                                  ┌──────────────────┐
                                  │ usa: trawler     │
                                  │ container        │ ──▶ restart with new image
                                  └──────────────────┘
```

---

## 4. GitHub Actions 构建

### 4.1 Workflow 文件草稿

**路径**：`.github/workflows/docker-publish.yml`

```yaml
name: Build & Publish Docker Image

on:
  push:
    branches: [master]
    tags: ['v*']           # v1.0.0 / v1.2.3 触发 release tag
    paths:
      - 'src/**'
      - 'shared/**'
      - 'platforms/**'
      - 'core/**'
      - 'pyproject.toml'
      - 'uv.lock'
      - 'Dockerfile'
      - 'docker/**'
      - '.github/workflows/docker-publish.yml'
  workflow_dispatch:       # 手动触发（debug 用）
    inputs:
      tag:
        description: '额外 tag（可选，如 rc1）'
        required: false

permissions:
  contents: read
  packages: write          # 推 GHCR 必需

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up QEMU (for arm64, see §10 Task 0 decision)
        if: vars.BUILD_ARM64 == 'true'
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}   # 内置 token, 无需新建 PAT

      - name: Docker meta (tags + labels)
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/yiny1/trawler
          tags: |
            type=ref,event=branch                    # master -> :master
            type=semver,pattern={{version}}          # v1.2.3 -> :1.2.3
            type=semver,pattern={{major}}.{{minor}}  # v1.2.3 -> :1.2
            type=sha,prefix=sha-,format=short        # :sha-abc1234
            type=raw,value=latest,enable={{is_default_branch}}  # master -> :latest

      - name: Build & Push
        uses: docker/build-push-action@v6
        with:
          context: .
          file: ./Dockerfile
          platforms: ${{ vars.BUILD_ARM64 == 'true' && 'linux/amd64,linux/arm64' || 'linux/amd64' }}
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha                       # GitHub Actions cache
          cache-to: type=gha,mode=max
          build-args: |
            DOWNLOAD_WHISPER_MODEL=0
          # whisper 预热放到运行时 HF_HOME volume, 不进镜像
```

**关键设计**：

1. **触发**：master push（源码/构建相关文件改动）+ tag（release）+ 手动
2. **token**：用 `GITHUB_TOKEN`（内置，自动生成），**不需要新建 PAT** 给 Actions
3. **tag 策略**：
   - `:latest` — master 最新（watchtower 跟踪这个）
   - `:sha-xxxxxxx` — 便于追溯 commit
   - `:1.2.3` / `:1.2` — semver release tag（手动 `git tag v1.2.3`）
   - `:master` — 分支名兜底
4. **cache**：`type=gha` 把 layer cache 存在 Actions cache 里，第二次构建 ~2-3min
5. **whisper 不进镜像**：`DOWNLOAD_WHISPER_MODEL=0`，靠 `trawler_hf_cache` volume 在首次转写时下载（与当前行为一致）

### 4.2 多架构决策

**默认仅 amd64**。理由：

- 服务器架构待 §10 Task 0 确认；若为 amd64 则 arm64 构建纯属浪费
- arm64 构建通过 QEMU 模拟，时长翻倍（~12-15min），且容易出兼容问题
- 若未来服务器换 arm（如 Oracle Cloud ARM 免费机），再开 `BUILD_ARM64=true` 变量

### 4.3 仓库可见性

GHCR 镜像继承仓库可见性。**当前仓库 public**，所以镜像默认 public — 任何人可 pull。

**风险**：镜像内不含 secret（cookies/LLM key 都在 config volume），但仍泄漏：依赖列表、代码结构。

**建议**：若担心可在 GHCR package settings 手动切 private（PAT 拉镜像需 `read:packages` scope）。

---

## 5. 服务器自动更新

### 5.1 推荐方式：Watchtower

**理由**：原生 Docker、声明式、自动清理旧镜像、支持 webhook。

**Compose 片段**（追加到 `/root/app/docker-compose.yml`）：

```yaml
services:
  watchtower:
    image: containrrr/watchtower:latest
    container_name: watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /root/.docker/config.json:/config.json:ro   # docker login 凭据（GHCR PAT）
    environment:
      # ── 拉取策略 ──
      WATCHTOWER_POLL_INTERVAL: 300           # 5 min 检查一次（见 §6 频率讨论）
      WATCHTOWER_CLEANUP: "true"              # 拉完删旧镜像
      WATCHTOWER_REMOVE_VOLUMES: "false"      # 保留 named volumes
      WATCHTOWER_LABEL_ENABLE: "true"         # 仅更新有 label 的容器（不误伤 caddy/gotify）
      WATCHTOWER_ROLLING_RESTART: "true"      # 一次重启一个，避免同时挂
      WATCHTOWER_TIMEOUT: "60s"               # stop grace period
      WATCHTOWER_HTTP_API_TOKEN: ${WATCHTOWER_API_TOKEN:-}   # 可选 webhook 立即触发
      WATCHTOWER_HTTP_API_UPDATE: "true"
    ports:
      - "127.0.0.1:9000:8080"                 # webhook 端口，仅 loopback
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "3"
```

**仅给 trawler 容器打 label**（防止 Caddy/Gotify/Relay 被误更新）：

```yaml
services:
  trawler:
    image: ghcr.io/yiny1/trawler:latest    # 从 trawler:local 改成这里
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
    # 删除 build: 段（不再本地构建）
    # 其余 volumes/env/healthcheck 不变
```

### 5.2 备选方式：cron + `docker compose pull`

**适用**：不想要 watchtower 容器、想完全自己掌控。

**脚本** `/root/app/update-trawler.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /root/app
OLD_ID=$(docker inspect --format='{{.Image}}' trawler 2>/dev/null || echo none)

docker compose pull trawler >/dev/null
NEW_ID=$(docker inspect --format='{{.index .RepoDigests 0}}' "ghcr.io/yiny1/trawler:latest" 2>/dev/null || echo none)

if [[ "$OLD_ID" == "$NEW_ID" ]]; then
  echo "[$(date)] no update"
  exit 0
fi

echo "[$(date)] updating: $OLD_ID -> $NEW_ID"
docker compose up -d trawler

# 健康检查 + 回滚（见 §7）
sleep 20
if ! curl -fsS http://127.0.0.1:8080/login >/dev/null; then
  echo "[$(date)] HEALTH CHECK FAILED, rolling back"
  docker tag "$OLD_ID" ghcr.io/yiny1/trawler:latest || true
  docker compose up -d trawler
  echo "[$(date)] rolled back to $OLD_ID"
  exit 1
fi
echo "[$(date)] update OK"
```

**cron**（`/etc/cron.d/trawler-update`）：
```
*/5 * * * * root /root/app/update-trawler.sh >> /var/log/trawler-update.log 2>&1
```

**对比**：

| 维度 | Watchtower | cron 脚本 |
|---|---|---|
| 配置复杂度 | 一段 compose | 一脚本 + crontab |
| 自动清理旧镜像 | ✅ 内置 | ❌ 需手动 `docker image prune` |
| 健康检查回滚 | ❌（v2 不支持，需自定义） | ✅ 可写脚本 |
| Webhook 立即触发 | ✅ | ❌ |
| 透明度 | 中（看 watchtower logs） | 高（脚本日志） |

**推荐 Watchtower**，**但若担心回滚能力**可走 cron 脚本路径（本 plan §7 给出回滚预案，watchtower 也能用）。

---

## 6. 自动更新频率

### 6.1 候选频率

| 频率 | 场景 | 评价 |
|---|---|---|
| 实时（webhook） | CI 推完镜像立即拉 | 最快，但需 GHCR webhook 或 Actions 调服务器 |
| 5 min | 开发期 / 频繁迭代 | watchtower polling，带宽可接受（只 check digest） |
| **15-30 min** | **生产推荐** | 平衡及时性与稳定性 |
| 24h（watchtower 默认） | 保守 | 太慢，违背「自动更新」初衷 |

### 6.2 推荐：5 min（开发期）→ 30 min（稳定后）

**理由**：

- watchtower poll 只查 manifest digest，**不下载 layer**，流量极小（每次几 KB）
- 真正下载只在新镜像存在时，一个月即便每天 5 次更新也就几十 MB
- 单人项目没人盯着，5 min 内能发现问题立即修

**生产期切 30 min**：等流程稳定后，编辑 compose `WATCHTOWER_POLL_INTERVAL: 1800`。

### 6.3 立即触发（可选）

Actions 推完镜像后调 watchtower webhook：

```yaml
# 在 docker-publish.yml 末尾追加
- name: Notify server to update
  if: github.ref == 'refs/heads/master'
  run: |
    curl -fsS -X POST \
      -H "Authorization: Bearer ${{ secrets.WATCHTOWER_API_TOKEN }}" \
      http://usa:9000/v1/update \
      || echo "webhook failed, will catch up on next poll"
```

需 Cloudflare/Caddy 把 `usa:9000` 暴露成 `https://trawl.priestess.host/hooks/watchtower`（仅 token），或服务器侧用 ngrok/tailscale。**初期不上 webhook，靠 5min polling 即可。**

---

## 7. 回滚预案

### 7.1 Watchtower 自动回滚（推荐）

Watchtower v2 本身**不支持**自动健康检查回滚。需要外挂：

**方案**：cron 每分钟检查 trawler 健康，不健康则回滚到上一次镜像。

`/root/app/trawler-rollback-watch.sh`：

```bash
#!/usr/bin/env bash
# 监控 trawler 健康，挂了自动回滚上一个镜像
set -euo pipefail

HEALTH=$(docker inspect --format='{{.State.Health.Status}}' trawler 2>/dev/null || echo none)
if [[ "$HEALTH" == "unhealthy" ]]; then
  echo "[$(date)] trawler unhealthy, attempting rollback"

  # watchtower 把前一个镜像 id 存在它的 cleanup 之前 — 我们要在 cleanup 前抓
  # 改 watchtower 配置 WATCHTOWER_CLEANUP=false 保留旧镜像更稳
  PREV=$(docker images --format '{{.ID}} {{.Repository}}:{{.Tag}}' \
         | grep 'ghcr.io/yiny1/trawler' \
         | awk '{print $1}' \
         | sed -n '2p')   # 第 2 行 = 倒数第 2 个 = 上一个

  if [[ -n "$PREV" ]]; then
    docker tag "$PREV" ghcr.io/yiny1/trawler:rollback
    docker compose up -d trawler   # 用 :rollback tag 临时跑
    echo "[$(date)] rolled back to $PREV"
  else
    echo "[$(date)] no previous image, manual recovery needed"
  fi
fi
```

**关键改动**：watchtower 配置需 **`WATCHTOWER_CLEANUP=false`**（保留旧镜像供回滚），改用每周一次的 `docker image prune --filter "until=168h"` cron 清理。

### 7.2 手动回滚（最稳）

任何自动机制都可能挂。**真正的保底是手动**：

```bash
# 1. 查看历史镜像
docker images ghcr.io/yiny1/trawler --format 'table {{.Tag}}\t{{.ID}}\t{{.CreatedAt}}'

# 2. 回滚到特定 sha tag（永远保留 sha tag 不被删）
docker tag ghcr.io/yiny1/trawler:sha-abc1234 ghcr.io/yiny1/trawler:latest
docker compose up -d trawler
```

**关键**：`:latest` 会被 watchtower 覆盖，但 `:sha-xxxxxxx` 永远不变 — 这是回滚锚点。

### 7.3 Worst Case：完全起不来

```bash
# SSH 上服务器
cd /root/app
docker compose logs trawler | tail -50   # 看错
docker compose stop trawler
# 回到上一个已知好的 sha
docker tag ghcr.io/yiny1/trawler:sha-<lastgood> ghcr.io/yiny1/trawler:latest
docker compose up -d trawler
# 验证
curl -fsS http://127.0.0.1:8080/login && echo OK
```

---

## 8. 认证与 Secret 管理

### 8.1 Actions → GHCR（push）

用 `GITHUB_TOKEN`（每个 workflow 自动注入），scope `packages: write`。**零额外 secret**。

### 8.2 服务器 → GHCR（pull）

**步骤**：

1. GitHub → Settings → Developer settings → PAT (classic) → 新建：
   - Scope：仅 `read:packages`
   - 过期：建议 1 年（到期前会邮件提醒）
   - Note：`usa-trawler-pull`
2. 服务器执行：
   ```bash
   echo "<PAT>" | docker login ghcr.io -u YinY1 --password-stdin
   ```
   凭据存到 `/root/.docker/config.json`（自动）
3. compose 里挂载 `/root/.docker/config.json:/config.json:ro` 给 watchtower

### 8.3 Secret 不进 git

**绝对禁止**：
- 把 PAT 写进 `docker-compose.yml`
- 把 PAT 写进仓库任何文件
- 把 PAT commit 到服务器上的 `/root/app/trawler/`（若该目录被 git 管理）

**正确**：
- PAT 仅存在 `/root/.docker/config.json`（root-only, 0600）
- watchtower 通过 volume 只读挂载

### 8.4 PAT 泄漏应急

```bash
# GitHub → Settings → Developer settings → PAT → Revoke
# 服务器重新生成新 PAT, docker login
# 镜像本身无需重建
```

GHCR 不缓存 token，撤销立即生效。

---

## 9. 迁移步骤

### Task 0 — 架构确认（前置）

```bash
ssh usa 'uname -m && docker version --format "{{.Server.Os}}/{{.Server.Arch}}"'
```

期望：`x86_64` / `linux/amd64`。若是 `aarch64`，Actions workflow 需开 `BUILD_ARM64=true`。

### Task 1 — GitHub Actions 配置

1. 在 repo 创建 `.github/workflows/docker-publish.yml`（§4.1）
2. push 到 master，观察 Actions tab — 应跑通并推到 `ghcr.io/yiny1/trawler:latest`
3. 在 GitHub → Packages 验证镜像存在
4. **可选**：在 package settings 改可见性为 private（§4.3）

**验证**：
- [ ] Actions 绿
- [ ] GHCR 上能看到 `:latest`、`:sha-xxxxxxx`、`:master` 三个 tag
- [ ] `docker pull ghcr.io/yiny1/trawler:latest` 在本机能拉

### Task 2 — 服务器 docker login

1. 生成 PAT（仅 `read:packages`）
2. `ssh usa`
3. `echo "<PAT>" | docker login ghcr.io -u YinY1 --password-stdin`
4. 验证：`docker pull ghcr.io/yiny1/trawler:latest`（应能拉到）
5. `ls -la /root/.docker/config.json`（应为 `-rw------- root root`）

**验证**：
- [ ] `docker pull` 成功
- [ ] config.json 权限 0600

### Task 3 — Compose 切换 image

1. `ssh usa`
2. 备份当前 compose：
   ```bash
   cp /root/app/docker-compose.yml /root/app/docker-compose.yml.bak.$(date +%Y%m%d)
   ```
3. 编辑 `/root/app/docker-compose.yml` 的 trawler 段：
   - `image: trawler:local` → `image: ghcr.io/yiny1/trawler:latest`
   - 删除 `build:` 段
   - 加 label：`com.centurylinklabs.watchtower.enable=true`
4. 第一次拉新镜像 + 重启：
   ```bash
   cd /root/app
   docker compose pull trawler
   docker compose up -d trawler
   ```
5. 健康检查：
   ```bash
   sleep 20
   curl -fsS http://127.0.0.1:8080/login && echo OK
   docker compose logs trawler | tail -20
   ```
6. web 端访问 https://trawl.priestess.host 验证页面正常

**验证清单**：
- [ ] 容器跑起来：`docker ps | grep trawler`
- [ ] healthcheck passing：`docker inspect trawler | grep HealthStatus`
- [ ] /login 返回 200
- [ ] web 页面能登录
- [ ] config volume 挂载正常（cookies/订阅未丢）

### Task 4 — 加 Watchtower

1. 在 `/root/app/docker-compose.yml` 追加 watchtower 段（§5.1）
2. `cd /root/app && docker compose up -d watchtower`
3. 等 5 min，看 watchtower 日志：
   ```bash
   docker logs watchtower --tail 50
   ```
   应看到：`Checking containers for updated images` / `No updates available`（因为刚拉过）
4. **测试自动更新**：
   - 本地改 README 触发一次 Actions（或手动 `workflow_dispatch`）
   - 等 Actions 跑完（~6min）
   - 等 watchtower 下次 poll（5min）
   - 看 `docker logs watchtower` 应有 `Found new image` / `Recreated container`
   - 验证 trawler 容器 ID 变了、健康

**验证清单**：
- [ ] watchtower 容器 running
- [ ] 5 min 内日志有 poll 记录
- [ ] 触发新构建后，trawler 容器自动重启
- [ ] 重启后 healthcheck 通过

### Task 5 — 回滚机制（可选但推荐）

1. 把 `WATCHTOWER_CLEANUP` 改成 `false`（保留旧镜像）
2. 部署 §7.1 的 cron 监控脚本
3. 测试：手动 `docker stop` trawler 或改坏 healthcheck，看是否触发回滚

**验证清单**：
- [ ] 旧镜像在 `docker images` 里保留
- [ ] 模拟 unhealthy 后，cron 触发回滚
- [ ] 回滚后容器恢复健康

### Task 6 — 清理（可选）

- 删除服务器 `/root/app/trawler/` 整个目录（git clone 出来的源码，不再需要）
- 服务器 `docker rmi trawler:local`（旧镜像）
- 卸载服务器上的 git（若没有其他用途）

---

## 10. Tasks Summary

| # | Task | Owner | Est | Blocks |
|---|---|---|---|---|
| 0 | 架构确认 `uname -m` | oracle/用户 | 2min | 全部 |
| 1 | GitHub Actions workflow + 首次构建 | @fixer | 30min | 2,3 |
| 2 | 服务器 docker login (PAT) | 用户 | 10min | 3,4 |
| 3 | Compose 切换 image + 验证 | @fixer | 30min | 4 |
| 4 | Watchtower 部署 + 自动更新测试 | @fixer | 45min | 5 |
| 5 | 回滚机制部署（可选） | @fixer | 30min | - |
| 6 | 清理旧源码目录 | 用户 | 10min | - |

**总计**：~3h（含等待构建/poll 时间）

---

## 11. Risks & Mitigations

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| GHCR rate limit（匿名 100 pull/6h，认证 200） | 低（单服务器单 pull） | 中 | 服务器已 docker login，走认证额度，远低于上限 |
| PAT 泄漏 | 中 | 中 | 细粒度（仅 `read:packages`），1 年过期，§8.4 应急流程 |
| Watchtower 拉到坏镜像导致 trawler 挂 | 中 | 高 | healthcheck + §7 回滚脚本；master 分支 PR review 把关 |
| GitHub Actions 故障 | 低 | 低 | 可手动 `docker build` 临时回退旧流程 |
| arm64 服务器用错架构镜像 | 低（前提 Task 0） | 高 | Task 0 必须先确认；workflow 默认 amd64 |
| whisper 首次转写卡（HF download） | 中 | 低 | hf_cache volume 持久化，仅首次慢；与现状一致 |
| GHCR 流量超额（私有镜像免费 1GB/月） | 极低 | 低 | 一次 pull ~50-200MB，月均 10-20 次 update 才会接近；公开镜像无限 |
| 旧镜像撑爆磁盘 | 中 | 中 | `WATCHTOWER_CLEANUP=true`（若不上回滚）或每周 `docker image prune` cron |

---

## 12. Open Decisions（需用户拍板）

1. **服务器架构**：Task 0 执行后填入。若 arm64，需在 Actions workflow 设 `BUILD_ARM64=true`，构建时间翻倍。
2. **镜像可见性**：public（默认）/ private？若 repo 已 public 且镜像不含 secret，public 无伤大雅。
3. **自动更新频率**：5 min（开发期，推荐）/ 30 min（稳定期）/ webhook（最实时但需额外配置）。
4. **回滚机制**：上 §7.1 cron 监控 / 仅靠手动 §7.2 / 跳过（接受风险）。
5. **Watchtower cleanup**：true（自动删旧，无法自动回滚）/ false（保留旧，磁盘占用大但可回滚）。
6. **服务器源码目录清理**：Task 6 是否执行（删了无法本地 build 兜底）。

---

## 13. Out of Scope（本 plan 不做）

- 多服务器 / 多地域部署（单服务器场景）
- 蓝绿 / 金丝雀发布（个人项目无必要）
- k8s / nomad / swarm（杀鸡用牛刀）
- 应用层健康指标上报（Prometheus 等）
- 镜像签名 / SBOM（cosign 等，可后续加）
- Cloudflare 缓存策略调整（不影响镜像层）

---

**End of plan**

---

## 用户决策（2026-06-25 拍板）

**整 plan 延后执行**。6 个 open decisions 留待后续逐项拍板。当前继续用 git pull + 本地 docker build 手工流程。
