# Web UI Redesign — Apple Soft Style

为 Trawler 现有 Web UI（FastAPI + HTMX，2026-06-15 引入）做视觉与交互层面的改头换面。**后端 13 条路由与异步架构保持不变**，本次只重写前端层（templates + CSS + 少量 JS）。

## Status

Approved. All sections confirmed by user (style/layout/accent/stack/scope + 5 page mockups).

## 决策摘要

| 维度 | 选择 | 理由 |
|---|---|---|
| 视觉方向 | **C · Apple 柔和友好** | 浅色 + 毛玻璃 + 大圆角 + 柔和阴影 |
| 布局结构 | **A · 左侧固定侧栏** | 桌面为主的后台工具、导航常驻、改造成本最低 |
| 强调色 | **Apple Blue `#0071e3`** | 最贴合 Apple 原生、中性、不与语义色冲突 |
| 技术栈 | **B · HTMX + Tailwind CSS（CDN）** | 路由不动、零构建、utility 表达毛玻璃最自然 |
| 暗色模式 | **跟随系统**（CSS 变量 + `prefers-color-scheme`） | 每色写双份 token，成本可接受 |
| 响应式 | **&lt;768px 侧栏折叠成汉堡** | 移动端可用 |
| 范围 | **5 页全部重写 + 新增 toast/loading/暗色/响应式**；不做消息详情页、实时刷新、i18n、多用户、趋势图表 | YAGNI |

## 语义色（双模式）

浅色 / 暗色：

- **Primary（accent）**: `#0071e3` / `#0a84ff`
- **Success**: `#34c759` / `#30d158`
- **Warning**: `#ff9500` / `#ff9f0a`
- **Error**: `#ff3b30` / `#ff453a`
- **Neutral 文字**: `#1d1d1f` / `#f5f5f7`
- **Neutral 次要**: `#6e6e73` / `#98989d`
- **背景基底**: `linear-gradient(135deg,#f5f7fa,#fff)` / `#000`
- **卡片底**: `rgba(255,255,255,.8)` + `shadow 0 4px 14px rgba(0,0,0,.05)` / `rgba(28,28,30,.8)` + 边框 `rgba(255,255,255,.06)`
- **侧栏底**: `rgba(255,255,255,.72)` + `backdrop-blur(20px)` / `rgba(28,28,30,.72)` + `backdrop-blur(20px)`
- **圆角**: 卡片 `14px`、按钮 `9-10px`、徽章 `10px`、字段 `8px`
- **字体**: `-apple-system, "SF Pro Display", sans-serif`；monospace 用 `ui-monospace, "SF Mono"`

## 技术栈细节

- **Tailwind 引入**：`<script src="https://cdn.tailwindcss.com"></script>`（CDN，零构建）
- **Tailwind config 注入**：在 `base.html` 内联 `tailwind.config = { darkMode: 'media', theme: { extend: { colors: {...Apple tokens...} } } }`
- **HTMX**：保留现有 CDN（`htmx.org@2.0.4`）
- **现有 `web/static/app.css`**：废弃 39 行单文件，改为 `web/static/tokens.css`（仅放无法用 Tailwind 表达的 CSS 变量与极少量自定义动画如 `blink`/`fadeIn`）
- **Jinja macros**：新建 `web/templates/_macros.html`，封装 `field`/`toggle`/`badge`/`stat_card` 等组件，避免每页重写

## 项目结构变更

```
web/
├── templates/
│   ├── base.html                  （改写：Tailwind + 侧栏 + 暗色变量）
│   ├── _macros.html               （新增：组件 macro 库）
│   ├── _candidates.html           （改写：候选人下拉样式）
│   ├── dashboard.html             （改写）
│   ├── subscriptions.html         （改写）
│   ├── check.html                 （改写）
│   ├── login.html                 （改写）
│   └── settings.html              （改写）
├── static/
│   ├── app.css                    （删除）
│   └── tokens.css                 （新增：CSS 变量 + 动画）
└── routes/                        （不动）
```

**新增的客户端 JS**：写在 `base.html` 底部 `<script>`，原生（不引框架），仅含：
- toast 系统（`showToast(msg, type)`）
- 表单提交 loading + disabled 反馈（HTMX `htmx:configRequest` 事件钩子）
- QR 模态控制（打开/关闭/步骤更新）
- 暗色模式无额外 JS（纯 CSS `@media (prefers-color-scheme: dark)`）
- 移动端汉堡菜单 toggle

## 页面设计

### 1. 仪表盘 `/`

- **顶栏**：页面标题 + "上次更新 X 分钟前"
- **统计卡片 4 列**：总消息（中性）/ 处理中（橙）/ 已完成（绿）/ 错误（红），数字着色
- **最近消息面板**：表格 + 阶段胶囊徽章（pushed=绿、summarized/downloading=蓝、transcribing=橙、error=红），时间格式化为 "14:32" 或 "昨天 14:32"
- **订阅概览**（保留）：平台名 + 计数

### 2. 订阅管理 `/subscriptions`

- **按平台分组卡片**（不再三段无视觉分隔）
- 每组：平台名 + 订阅数徽章（蓝）+ 最后抓取时间
- **搜索为主交互**：搜索框 → `hx-post="/subscriptions/search"` 注入候选人下拉 → 点 "+ 添加" 触发 add
- 候选人下拉：名称 / UID / URL 三段，hover 蓝色高亮
- 行内"手动添加"折叠区（输入 UID + 名称），覆盖搜不到的场景
- 订阅行：状态小圆点（绿=活跃、橙=待验证、红=失效）
- 删除：红色链接 + 确认弹窗（不再整块红按钮），HTMX `hx-delete`
- 平台组支持折叠/展开

### 3. 内容检查 `/check`

- **状态面板**（替代孤零零按钮）：状态指示（运行中=橙 / 空闲=灰 / 完成=绿）+ 已耗时 + 已处理条数 + 运行按钮
- 运行中按钮变橙色禁用（"运行中…"），完成后变绿"再次运行"
- **5 段进度条**（按 phase 流：discover→download→transcribe→summarize→push），运行中段橙色，已完成绿，未开始灰
- **终端日志区保留深色**（即使浅色模式），符合开发者直觉
- 日志结构：`[时间] [级别] [平台]` + 内容；级别着色（INFO 灰、OK 绿、WARN 橙、ERR 红）
- 平台标签蓝色高亮 `[bilibili]` 便于扫读
- 顶部"全部 / 仅错误 / LIVE"切换：客户端过滤（不增加后端调用）+ 自动滚动到底
- 光标闪烁强化"实时"感
- **后端 SSE 路由 `/check/stream` 完全不动**，保留 `asyncio.Queue` 解耦

### 4. 登录管理 `/auth`

- **Token 状态卡片网格（3 列）**替代现状表格：每张卡 = 一个平台
- 状态圆点三色（绿=有效、红=过期、灰=未配置）+ 剩余时间
- 按钮语境化：
  - 有效 → `[查看]`（ghost）+ `[续期]`（primary）
  - 过期 → `[扫码续期]`（primary）
  - 未配置 → `[扫码登录]`（primary）
- **QR 扫码改模态弹窗**（不再挤在表格底下），含半透明遮罩 + 居中卡片
- **4 步进度**：二维码生成 → 等待扫描 → 确认登录 → 保存凭证；当前蓝、已完成绿、未来灰
- 二维码倒计时（橙色提示），过期自动重新请求 `/auth/qr/{key}`
- 轮询保留后端 `/auth/poll/{key}`，前端只更新 step 文字，不再 reload 整页
- 成功后 toast + 自动关闭模态 + 刷新对应卡片

### 5. 设置 `/settings`

- **分组卡片**替代裸表单：常规 / 下载 / 转写 / 通知 / 平台启用 — 每组带 icon + 一句话描述
- 字段网格 2 列布局，长字段（路径/URL）占满宽
- **toggle 开关**（Apple 风）替代 checkbox — SSL 验证、平台启用都用它
- 平台启用单独一组：名称 + 描述 + 开关
- **sticky 底部保存栏**：检测到 dirty 状态时橙色提示"有未保存的更改" + `[放弃]` `[保存]`
- 保存：HTMX `hx-post="/settings"` → toast 成功提示（不再 redirect flash）
- 敏感字段（Gotify token）`type=password`，占位符掩码
- focus 态：蓝色边 + 半透明蓝色光晕
- 验证错误：input 红边 + 字段下方红字（不再跳 flash msg）

## 不在本次范围

- 消息详情页（需要新路由 + 详情数据）
- 仪表盘实时刷新（HTMX 轮询会一直打后端，先手动刷新）
- 国际化（中文一种）
- 多用户 / 权限（个人工具）
- 历史趋势图表（数据没存历史）
- Tailwind 改本地构建（CDN 已够用，将来要优化再做）

## 实现顺序建议（给 writing-plans）

1. 基础设施：`base.html` 改 Tailwind + Apple tokens + 暗色变量；`tokens.css` + `_macros.html`
2. 全局组件：toast 系统 + 表单 loading + 汉堡菜单
3. 5 页依次改写：dashboard → subscriptions → check → auth → settings
4. 路由层微调（仅必要的：例如 settings 加 HTMX 提交端点、check/run 返回 HTML 片段替代 JSON）
5. 验证：手动 5 页过一遍 + `uv run pytest`（已有 web 测试）

## 验证

- `uv run pytest tests/test_web_*.py` 全绿
- 手动过 5 页：浅色 / 暗色 / 移动端汉堡各一次
- 浏览器开发者工具切 `prefers-color-scheme` 验证暗色
- 触发一次 check 验证 SSE 流与日志着色
- 触发一次扫码验证 QR 模态与轮询
