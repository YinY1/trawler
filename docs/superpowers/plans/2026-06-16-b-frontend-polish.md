# Plan: B 组前端改进（订阅布局/仪表盘瘦身/toast 补全）

## 背景
3 处独立的前端微调，分别涉及布局优化（订阅 grid）、信息密度调整（仪表盘胖行瘦身）、体验补全（QR 关闭 toast + 删死按钮）。纯模板改动，不涉及后端路由或业务逻辑。

## 范围
| 文件 | 改动类型 |
|------|----------|
| `web/templates/subscriptions.html` | 布局（外层容器 + 卡片间距） |
| `web/templates/dashboard.html` | 删除 token 行 + 消息行改用 inline 紧凑卡片 |
| `web/templates/login.html` | closeQRModal 加 toast + 删"查看"死按钮 |

**不改动（但确认过）**：
- `web/routes/dashboard.py` — token 传参保留不删
- `web/templates/_macros.html` — `stat_card` 宏不用改，消息行直接用 inline HTML
- `web/templates/base.html` — toast 机制已完备（L80-96）

## 决策摘要
| 决策点 | 选项 | 选定 |
|--------|------|------|
| 订阅页多列方案 | ① CSS columns ② flex-wrap ③ grid | **grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4**（Tailwind 原生支持，降级自然） |
| 仪表盘消息行瘦身 | ① 改 stat_card macro 加 compact 参数 ② 直接 inline | **直接 inline**（stat_card 只在此处使用，改 macro 无收益，inline 更清晰） |
| QR 关闭检测实现 | ① 新增全局变量 qrSuccess ② 检查 qr-status 文本 ③ 检查 step 颜色 | **检查 document.getElementById('qr-status').textContent 是否含"成功"**（零新增状态，与现有实现一致） |

## 任务清单

### Task 1: 订阅布局 grid 化

- **文件**: `web/templates/subscriptions.html`
- **当前**（行 10-11）: 每个平台卡片独立 `{% for p in platforms %}` → `<div class="... mb-4 overflow-hidden">`，卡片纵向堆叠，靠 `mb-4` 产生间距。
- **目标**: 平台卡片按 1/2/3 列响应式网格排列，移除 `mb-4`，改为 `gap-4`。
- **改动**:

  1. **行 10**: 在 `{% for p in platforms %}` 外面包裹一个 grid 容器。
     ```html
     <!-- Before (L10) -->
     {% for p in platforms %}
     <div class="bg-[var(--card-bg)]... mb-4 overflow-hidden">
     
     <!-- After -->
     <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
     {% for p in platforms %}
     <div class="bg-[var(--card-bg)]... overflow-hidden">
     ```
  2. **行 74**: 在 `{% endfor %}`（platforms 循环结束）后添加 `</div>` 关闭 grid 容器。
     ```html
     <!-- Before (L74-75) -->
     </div>
     {% endfor %}
     
     <!-- After -->
     </div>
     {% endfor %}
     </div>
     ```
  3. **行 11**: 移除平台卡片的 `mb-4`：
     ```html
     <!-- Before -->
     <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)] mb-4 overflow-hidden">
     <!-- After -->
     <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)] overflow-hidden">
     ```

- **验证**: `curl http://localhost:8000/subscriptions` 返回 HTTP 200，3 列网格布局正常。

---

### Task 2: 仪表盘瘦身

- **文件**: `web/templates/dashboard.html`
- **改动**:

  **2a. 删除 token 状态行（行 20-25）**

  删除整个 `<div>` 块：
  ```html
  <!-- 删除以下 6 行 -->
  <!-- Stats row 2: token status -->
  <div class="grid grid-cols-3 gap-4 mb-6">
    {{ stat_card(token_ok, "Token 有效", "text-green-500") }}
    {{ stat_card(token_expired, "Token 过期", "text-red-500") }}
    {{ stat_card(token_none, "未配置 Token", "text-[var(--text-tertiary)]") }}
  </div>
  ```
  删除后 `{% if sub_counts %}`（当前行 27）变为紧跟在消息行后面。

  **2b. 消息计数行改为紧凑 inline 卡片（行 12-18）**

  - 容器从 `grid grid-cols-2 md:grid-cols-4 gap-4 mb-4` 改为 `flex flex-wrap gap-3 mb-4`
  - 每个 `{{ stat_card(...) }}` 调用替换为手工 inline HTML 卡片（不使用 macro）

  ```html
  <!-- Before (L12-18) -->
  <!-- Stats row 1: message counts -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
    {{ stat_card(total_msgs, "总消息", "text-[var(--text-primary)]") }}
    {{ stat_card(active_count, "处理中", "text-orange-500") }}
    {{ stat_card(pushed_count, "已完成", "text-green-500") }}
    {{ stat_card(error_count, "错误", "text-red-500") }}
  </div>

  <!-- After -->
  <!-- Stats row 1: message counts (compact) -->
  <div class="flex flex-wrap gap-3 mb-4">
    <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[10px] px-3 py-1.5 shadow-[0_2px_8px_rgba(0,0,0,0.04)] border border-[var(--card-border)] flex items-center gap-2">
      <span class="text-sm font-semibold tracking-tight text-[var(--text-primary)]">{{ total_msgs }}</span>
      <span class="text-xs text-[var(--text-secondary)]">总消息</span>
    </div>
    <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[10px] px-3 py-1.5 shadow-[0_2px_8px_rgba(0,0,0,0.04)] border border-[var(--card-border)] flex items-center gap-2">
      <span class="text-sm font-semibold tracking-tight text-orange-500">{{ active_count }}</span>
      <span class="text-xs text-[var(--text-secondary)]">处理中</span>
    </div>
    <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[10px] px-3 py-1.5 shadow-[0_2px_8px_rgba(0,0,0,0.04)] border border-[var(--card-border)] flex items-center gap-2">
      <span class="text-sm font-semibold tracking-tight text-green-500">{{ pushed_count }}</span>
      <span class="text-xs text-[var(--text-secondary)]">已完成</span>
    </div>
    <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[10px] px-3 py-1.5 shadow-[0_2px_8px_rgba(0,0,0,0.04)] border border-[var(--card-border)] flex items-center gap-2">
      <span class="text-sm font-semibold tracking-tight text-red-500">{{ error_count }}</span>
      <span class="text-xs text-[var(--text-secondary)]">错误</span>
    </div>
  </div>
  ```

  **注意**: 删除 token 行后，模板不再引用 `token_ok`/`token_expired`/`token_none`。后端 `dashboard.py` 中这三个变量的计算和传入（L32-45, L64-66）**保留不动**，仅前端不渲染。

- **验证**: `curl http://localhost:8000/` 返回 HTTP 200，无 token 行，消息 card 紧凑。

---

### Task 3: login QR 失败 toast + 删死按钮

- **文件**: `web/templates/login.html`
- **改动**:

  **3a. closeQRModal 加关闭前检查（行 202-209）**

  在关闭 interval 之前检查 QR 当前状态。如果 QR 状态文本不含"成功"（即手动关闭/超时/失败），先显示 warning toast。

  ```javascript
  // Before (L202-209)
  function closeQRModal() {
    var overlay = document.getElementById('qr-overlay');
    overlay.classList.add('hidden');
    overlay.classList.remove('flex');
    if (qrPollInterval) { clearInterval(qrPollInterval); qrPollInterval = null; }
    if (qrCountdownInterval) { clearInterval(qrCountdownInterval); qrCountdownInterval = null; }
    qrPlatform = null;
  }

  // After
  function closeQRModal() {
    // 仅在非 success 关闭时提示（success 时 handlePollStatus 已显示成功 toast）
    var statusEl = document.getElementById('qr-status');
    if (statusEl && statusEl.textContent.indexOf('成功') === -1) {
      showToast('登录未完成', 'warning');
    }
    var overlay = document.getElementById('qr-overlay');
    overlay.classList.add('hidden');
    overlay.classList.remove('flex');
    if (qrPollInterval) { clearInterval(qrPollInterval); qrPollInterval = null; }
    if (qrCountdownInterval) { clearInterval(qrCountdownInterval); qrCountdownInterval = null; }
    qrPlatform = null;
  }
  ```

  **3b. 删除"查看"死按钮（行 33-34）**

  删除无 `onclick` 的"查看"按钮。该按钮在 token 有效状态下显示。

  ```html
  <!-- Before (L32-35) -->
  <div class="flex gap-2">
    {% if "有效" in p.token_status %}
    <button class="px-3 py-1.5 text-xs rounded-[8px] border border-gray-300 dark:border-gray-600 text-[var(--text-secondary)] hover:bg-gray-50 dark:hover:bg-gray-800/30 transition-colors">查看</button>
    <button onclick="openQRModal('{{ p.key }}')" class="px-3 py-1.5 text-xs rounded-[8px] bg-apple-blue text-white hover:bg-blue-600 transition-colors">续期</button>

  <!-- After (L32-35) -->
  <div class="flex gap-2">
    {% if "有效" in p.token_status %}
    <button onclick="openQRModal('{{ p.key }}')" class="px-3 py-1.5 text-xs rounded-[8px] bg-apple-blue text-white hover:bg-blue-600 transition-colors">续期</button>
  ```

  即删除 `<button class="px-3 py-1.5... hover:bg-gray-50...">查看</button>` 整行（不含后面的换行残留）。

- **验证**: 
  - `curl http://localhost:8000/auth` HTTP 200
  - Token 有效平台卡片内不再显示"查看"按钮
  - 手动点击 QR 模态框外部/关闭按钮，若扫码未完成则弹出 warning toast

---

## 验证清单

- [ ] `uv run ruff check .` 通过（模板文件不涉及 Python lint，但仍需确保无遗留问题）
- [ ] 启动 web 服务，以下路由都 HTTP 200：
  - `curl http://localhost:8000/`（仪表盘瘦身）
  - `curl http://localhost:8000/subscriptions`（grid 布局）
  - `curl http://localhost:8000/auth`（无死按钮）
- [ ] 视觉验证：
  - 订阅页平台卡片成 1/2/3 列网格，卡片间 gap 均匀
  - 仪表盘无 token 统计行，消息行 card 高度约 32px（`py-1.5`），不再是 80px+ 大卡片
  - login 页 token 有效时只有"续期"按钮，无"查看"
  - QR 模态框在非 success 状态关闭时弹出 warning toast

## 风险

1. **订阅页 grid 容器闭合位置** — 若 `{% for p in platforms %}` 和 `{% endfor %}` 之间有其他元素（JS script 块在循环外，不影响）。grid 容器只包裹卡片循环，script 块（L77-88）保持在外面。注意不要多闭合或漏闭合 `</div>`。
2. **stat_card macro 无其他调用者** — 已确认只有 dashboard 中使用。不改 macro 就不会影响其他页面。
3. **dashboard.py token 变量保留** — 后端代码不动，仅模板不渲染，无运行时错误。
4. **closeQRModal 重复调用** — `handlePollStatus` 在 success 时也调用了 `closeQRModal`（L188），加 toast 检查需确保 success 路径不触发 warning。实现方案用 `textContent.indexOf('成功') === -1` 可区分：success 路径的 `qr-status` 在调用 closeQRModal 之前已被设为 `'✅ 登录成功'`，因此不会触发 warning。
