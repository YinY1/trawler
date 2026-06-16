# Implementation Plan: Web UI Redesign (Apple Soft Style)

**Spec**: `docs/superpowers/specs/2026-06-16-web-ui-redesign-design.md`
**Branch**: `feat/web-ui-redesign`
**Total Tasks**: 9

---

**实现顺序**: Task 1 → 2 → 3 → (4, 5, 6, 7, 8 可并行) → 9
**依赖**: Task 4/5/6/7/8 都依赖 Task 1 (base.html) + 2 (macros) + 3 (全局 JS)；Task 9 是最终验证。

**注意**: Task 1 完成后页面会短暂 broken（base.html 重写但子页面仍引用旧 macros/类名）。
Task 4-8 是 5 个独立页面改写，可并行进行；每完成一个页面，该页面即恢复可用。
Task 9 全部完成后系统完全可用。

## Task 1: Infrastructure — base.html + tokens.css + app.py filter

**Goal**: Replace the 23-line barebones `base.html` with Tailwind CDN + Apple design tokens + fixed sidebar + dark mode CSS variables + mobile hamburger scaffold + bottom JS container. Create `tokens.css` (CSS variables + animations). Delete `app.css`. Register `timeago` Jinja filter.

**Files**:
- `web/templates/base.html` — rewrite
- `web/static/tokens.css` — create
- `web/static/app.css` — delete
- `web/app.py` — add `timeago` filter to `TEMPLATES`

**Steps**:

1. **Create `web/static/tokens.css`**:
   - Define `:root` CSS variables for all 20+ design tokens (Apple blue `#0071e3`, semantic colors, neutral text colors, background gradients, card/sidebar glass colors, border radii, font stacks) matching spec §语义色（双模式）
   - Add `@media (prefers-color-scheme: dark)` block overriding each token to dark variants (accent `#0a84ff`, bg black, card `rgba(28,28,30,.8)`, sidebar `rgba(28,28,30,.72)`)
   - Define `@keyframes fadeIn` and `@keyframes blink` (cursor blink for check page)
   - Keep under 60 lines — only what Tailwind utility classes cannot express

2. **Rewrite `web/templates/base.html`**:
   - Add Tailwind CDN `<script src="https://cdn.tailwindcss.com"></script>` after meta viewport
   - Add `<script>` block after Tailwind CDN with inline `tailwind.config = { darkMode: 'media', theme: { extend: { colors: { apple: { blue: '#0071e3', ... all semantic tokens } } } } }`
   - Link `tokens.css`: `<link rel="stylesheet" href="/static/tokens.css">`
   - Set `<html lang="zh-CN" class="h-full">`
   - **Body layout**: `<body class="h-full bg-[var(--bg-base)] text-[var(--text-primary)] font-sans antialiased">`
   - **Sidebar**: `<aside id="sidebar" class="fixed left-0 top-0 h-full w-60 bg-[var(--sidebar-bg)] backdrop-blur-[20px] border-r border-[var(--sidebar-border)] z-40 ...">` with:
     - Logo/title: "Trawler" in Apple SF Pro Display style
     - 5 nav links with SVG icons (no emoji per spec) + `active_nav` highlight using Apple blue
     - `<nav class="flex flex-col gap-1 px-3 mt-6">`
   - **Hamburger button**: `<button id="menu-toggle" class="md:hidden fixed top-3 left-3 z-50 ...">☰</button>`, hidden on `md:` and above
   - **Mobile overlay**: `<div id="sidebar-overlay" class="fixed inset-0 bg-black/30 z-30 hidden md:hidden" onclick="toggleSidebar()"></div>`
   - **Main content**: `<main id="main-content" class="ml-60 md:ml-60 p-6 md:p-8 min-h-screen transition-all duration-300">` with `{% block content %}{% endblock %}`
   - Swap emoji nav labels for SVG icon + text (or use Unicode symbols if user objects; per spec "no emoji" is not stated for nav, but design has SVG icons — use simple SVG inline for cleanliness)
   - **Bottom JS container**: `<script> ... </script>` block before `</body>` — initially empty `// Global JS loaded from tasks below`, as a placeholder

3. **Delete `web/static/app.css`**: Remove the file (no replacement needed — all styles move to Tailwind + tokens.css)

4. **Register `timeago` filter in `web/app.py`**:
   - After `TEMPLATES = Jinja2Templates(...)` line, add:
     ```python
     from datetime import datetime
     def _timeago(ts: float) -> str:
         """Format Unix timestamp as relative time string."""
         now = datetime.now().timestamp()
         diff = now - ts
         if diff < 3600:
             return f"{int(diff // 60)} 分钟前"
         elif diff < 7200:
             return "1 小时前"
         elif diff < 86400:
             return f"{int(diff // 3600)} 小时前"
         elif diff < 172800:
             return "昨天 " + datetime.fromtimestamp(ts).strftime("%H:%M")
         elif diff < 259200:
             return "2 天前"
         else:
             return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
     TEMPLATES.env.filters["timeago"] = _timeago
     ```
   - Add `from datetime import datetime` import at top

**Verification**:
- `uv run python run_web.py` starts without ImportError
- Visit `http://localhost:8080/` — see empty skeleton with Apple-style sidebar (left, fixed, frosted glass), main content area on right
- `curl http://localhost:8080/ | grep -c tailwind` returns > 0
- Browser DevTools > Elements: Tailwind classes applied, `tokens.css` loaded
- Toggle `prefers-color-scheme: dark` in DevTools > Rendering — sidebar and background colors change

---

## Task 2: Jinja Macros Library

**Goal**: Create reusable Apple-style component macros to avoid repetitive markup across 5 pages.

**File**: `web/templates/_macros.html` — create

**Steps**:

1. **Create `web/templates/_macros.html`** with the following macros (each thoroughly parameterized):

   - `{% macro stat_card(value, label, color="text-[var(--text-primary)]", icon="") %}` — renders a stat card div with large value number + label + optional color class. Structure:
     ```
     <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)]">
       {{ icon }}<div class="text-3xl font-semibold {{ color }}">{{ value }}</div>
       <div class="text-sm text-[var(--text-secondary)] mt-1">{{ label }}</div>
     </div>
     ```

   - `{% macro badge(text, color="blue") %}` — phase/tag capsule badge. Colors map: `blue` → `bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300`, `green` → `bg-green-100...`, `orange` → `bg-orange-100...`, `red` → `bg-red-100...`, `gray` → `bg-gray-100...`

   - `{% macro platform_tag(name) %}` — pill badge for platform names like `[bilibili]`, blue highlighted (spec: blue high-contrast). Renders `<span class="inline-flex items-center px-2 py-0.5 rounded-[10px] text-xs font-medium bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 font-mono">{{ name }}</span>`

    - `{% macro toggle(name, checked=False, label="") %}` — Apple-style toggle switch: hidden input sends `"false"` when unchecked, checkbox sends `"true"` when checked:
      ```
      <label class="relative inline-flex items-center cursor-pointer">
        <input type="hidden" name="{{ name }}" value="false">
        <input type="checkbox" name="{{ name }}" value="true" class="sr-only peer" {% if checked %}checked{% endif %}>
        <div class="w-9 h-5 bg-gray-200 peer-focus:ring-2 peer-focus:ring-blue-300 rounded-full peer transition-colors peer-checked:bg-blue-600 after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-transform peer-checked:after:translate-x-4"></div>
        <span class="ml-3 text-sm">{{ label }}</span>
      </label>
      ```

   - `{% macro field(name, value="", label="", type="text", placeholder="", required=False, width="full") %}` — labeled input field with error slot:
     ```
     <div class="flex flex-col gap-1.5 {{ 'col-span-2' if width == 'full' else '' }}">
       <label class="text-sm font-medium text-[var(--text-secondary)]">{{ label }}</label>
       <input type="{{ type }}" name="{{ name }}" value="{{ value }}" placeholder="{{ placeholder }}"
         class="px-3 py-2 rounded-[8px] border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-[var(--text-primary)] focus:ring-2 focus:ring-apple-blue/30 focus:border-apple-blue outline-none transition-all duration-200 {{ 'w-full' if width == 'full' else '' }}">
       <div class="text-xs text-red-500 hidden field-error"></div>
     </div>
     ```

   - `{% macro progress_segment(label, status="pending") %}` — one segment of the 5-phase progress bar. Status values: `done` (green), `running` (orange), `pending` (gray)

   - `{% macro status_dot(status="inactive") %}` — small colored dot. Green (active), orange (pending), red (error), gray (inactive)

2. **Add import guard**: `{% if not _macros_loaded %}{% set _macros_loaded = True %}{% endif %}` — not strictly needed since Jinja macros are lazily imported, but add a comment header documenting usage

**Verification**:
- Temporarily add to `dashboard.html`: `{% from "_macros.html" import stat_card %}` + `{{ stat_card(42, "测试", "text-green-500") }}`
- Refresh dashboard — see a nicely styled stat card rendered
- Remove the test code after verification

---

## Task 3: Global Client-Side JavaScript

**Goal**: Add toast system, HTMX loading hooks, mobile sidebar toggle, and QR modal helpers to `base.html`.

**File**: `web/templates/base.html` — add JS inside existing `<script>` block at bottom

**Steps**:

1. **Toast system** (`showToast(msg, type)`):
   ```javascript
   function showToast(msg, type) {
     var colors = { success: 'bg-green-500', error: 'bg-red-500', warning: 'bg-orange-500', info: 'bg-blue-500' };
     var t = document.createElement('div');
     t.className = 'fixed top-4 right-4 z-[9999] px-4 py-3 rounded-[10px] text-white text-sm font-medium shadow-lg ' + (colors[type] || colors.info) + ' animate-fadeIn';
     t.textContent = msg;
     document.body.appendChild(t);
     setTimeout(function() { t.style.opacity = '0'; t.style.transition = 'opacity 0.3s'; setTimeout(function() { t.remove(); }, 300); }, 3000);
   }
   ```

2. **HTMX request/response hooks** (loading state on buttons):
   ```javascript
   document.addEventListener('htmx:beforeRequest', function(e) {
     var btn = e.detail.elt.querySelector('button[type="submit"], button[data-loading]') || e.detail.elt.closest('button');
     if (btn) { btn.disabled = true; btn.dataset.origText = btn.textContent; btn.innerHTML = '<span class="inline-block w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin mr-2"></span>' + btn.textContent; }
   });
   document.addEventListener('htmx:afterRequest', function(e) {
     var btn = e.detail.elt.querySelector('button') || e.detail.elt.closest('button');
     if (btn && btn.dataset.origText) { btn.disabled = false; btn.textContent = btn.dataset.origText; delete btn.dataset.origText; }
   });
   document.addEventListener('htmx:responseError', function(e) {
     showToast('请求失败: ' + (e.detail.xhr.statusText || '网络错误'), 'error');
   });
   ```

3. **HTMX toast trigger listener** (listen for `HX-Trigger` JSON with toast):
   ```javascript
   document.addEventListener('htmx:afterOnLoad', function(e) {
     var trigger = e.detail.xhr.getResponseHeader('HX-Trigger');
     if (trigger) {
       try { var data = JSON.parse(trigger); if (data.toast) { showToast(data.toast.msg, data.toast.type || 'info'); } } catch(_) {}
     }
   });
   ```

4. **Mobile sidebar toggle**:
   ```javascript
   function toggleSidebar() {
     document.getElementById('sidebar').classList.toggle('-translate-x-full');
     document.getElementById('sidebar-overlay').classList.toggle('hidden');
   }
   document.getElementById('menu-toggle').addEventListener('click', toggleSidebar);
   document.getElementById('sidebar-overlay').addEventListener('click', toggleSidebar);
   ```
   Also add a `resize` listener: on window > 768px, ensure sidebar is visible and overlay hidden.

5. **QR modal helpers** (placeholder — full implementation in Task 7):
   ```javascript
   function openQRModal(platform) { /* filled in Task 7 */ }
   function closeQRModal() { /* filled in Task 7 */ }
   ```

**Verification**:
- Open browser console, call `showToast("Hello", "success")` — toast appears top-right, fades after 3s
- Call `showToast("Error", "error")` — red toast appears
- Click hamburger (resize to <768px) — sidebar slides out/in
- Submit any HTMX form — button shows spinner + disabled, restores after response

---

## Task 4: Dashboard Page Rewrite

**Goal**: Rewrite `web/templates/dashboard.html` with Apple-style stat cards (4 columns), phase badge table, subscription overview, timeago formatting, and Token status row.

**File**: `web/templates/dashboard.html` — rewrite

**Steps**:

1. **Import macros**: `{% from "_macros.html" import stat_card, badge, platform_tag %}`
2. **Page title**: `<h1 class="text-2xl font-semibold text-[var(--text-primary)] mb-1">仪表盘</h1>` with subtitle `<p class="text-sm text-[var(--text-secondary)] mb-6">上次更新 · {{ last_updated | timeago if last_updated else "—" }}</p>`
   - Note: `last_updated` is not currently provided by dashboard route — add `last_updated` to the template context in `web/routes/dashboard.py` (pass the latest message pubdate; Design says "上次更新 X 分钟前"). Use `"last_updated": recent[0].pubdate if recent else 0` (dashboard route already computes `recent = sorted(all_msgs, ...)[:20]`, avoiding a second sort).
3. **Stat cards row**: 4-column grid:
   ```html
   <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
     {{ stat_card(total_msgs, "总消息", "text-[var(--text-primary)]") }}
     {{ stat_card(active_count, "处理中", "text-orange-500") }}
     {{ stat_card(pushed_count, "已完成", "text-green-500") }}
     {{ stat_card(error_count, "错误", "text-red-500") }}
   </div>
   ```
4. **Token status row** (as 3 smaller stat cards): same pattern, using `stat_card` with smaller sizing
5. **Subscription overview**: card with platform list:
   ```html
   <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[...] border border-[var(--card-border)] mb-6">
     <h2 class="text-lg font-semibold mb-4">订阅概览</h2>
     <div class="flex flex-wrap gap-3">
       {% for platform, count in sub_counts.items() %}
       <div class="flex items-center gap-2 px-3 py-1.5 rounded-[10px] bg-gray-50 dark:bg-gray-800/50">
         {{ platform_tag(platform) }}<span class="text-sm font-medium">{{ count }}</span>
       </div>
       {% endfor %}
     </div>
   </div>
   ```
6. **Recent messages card**: table with styled header, phase badge, timeago:

   ```html
   <div class="bg-[var(--card-bg)] ... rounded-[14px] ... overflow-hidden">
     <div class="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
       <h2 class="text-lg font-semibold">最近消息</h2>
     </div>
     {% if recent_messages %}
     <div class="overflow-x-auto">
       <table class="w-full text-sm">
         <thead><tr class="text-[var(--text-secondary)] text-xs uppercase tracking-wider">
           <th class="text-left px-5 py-3 font-medium">时间</th><th class="text-left px-5 py-3 font-medium">平台</th>
           <th class="text-left px-5 py-3 font-medium">标题</th><th class="text-left px-5 py-3 font-medium">作者</th>
           <th class="text-left px-5 py-3 font-medium">阶段</th>
         </tr></thead>
         <tbody>
           {% for msg in recent_messages %}
           <tr class="border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50/50 dark:hover:bg-gray-800/30 transition-colors">
             <td class="px-5 py-3 whitespace-nowrap text-[var(--text-secondary)]">{{ msg.pubdate | timeago }}</td>
             <td class="px-5 py-3">{{ platform_tag(msg.platform) }}</td>
             <td class="px-5 py-3 max-w-xs truncate">{{ msg.title }}</td>
             <td class="px-5 py-3 text-[var(--text-secondary)]">{{ msg.author }}</td>
             <td class="px-5 py-3">{{ badge(msg.phase.name, phase_color(msg.phase)) }}</td>
           </tr>
           {% endfor %}
         </tbody>
       </table>
     </div>
     {% else %}
     <div class="p-8 text-center text-[var(--text-secondary)]">暂无消息</div>
     {% endif %}
   </div>
   ```

7. **Add `phase_color` filter** to app.py: a simple mapping `Phase.PUSHED → "green"`, `Phase.SUMMARIZED → "blue"`, etc. Register as `TEMPLATES.env.filters["phase_color"]`.

8. **Update `web/routes/dashboard.py`**: pass `last_updated` to context (computed from `recent[0].pubdate` if available).

**Verification**:
- Visit `/` — stat cards in 4 columns, numbers colored correctly
- Recent messages table with colored phase badges and relative time
- Toggle dark mode — all colors adapt
- Token stat cards show correct counts

---

## Task 5: Subscriptions Page Rewrite

**Goal**: Rewrite `web/templates/subscriptions.html` and `web/templates/_candidates.html` with platform-grouped cards, search-driven interaction, inline manual add, status dots, HTMX delete with confirm.

**Files**:
- `web/templates/subscriptions.html` — rewrite
- `web/templates/_candidates.html` — rewrite

**Steps**:

1. **Rewrite `subscriptions.html`**:
   - Import: `{% from "_macros.html" import badge, platform_tag, status_dot, field %}`
   - Page header with title + total count summary
   - **Flash message removal**: remove the old flash `{% if flash_msg %}` block — replace with toast-trigger approach
   - **Add/remove route update**: The current add/remove returns 303 redirect. Since we're using HTMX, we want these to return HX-Trigger toast + refresh the page content instead. However, to minimize backend changes, keep the redirect approach but add JS to capture flash params from URL and convert to toast on page load. Simpler approach: **keep 303 redirect for add/remove**, and add a small JS snippet that reads `?msg=` from URL and calls `showToast()` on pageload. This avoids route changes for now.
   - **For each platform group**:
     ```html
     <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] shadow-[...] border border-[var(--card-border)] mb-6 overflow-hidden">
       <!-- Header: collapsible -->
       <div class="flex items-center justify-between px-5 py-4 cursor-pointer" onclick="this.nextElementSibling.classList.toggle('hidden')">
         <div class="flex items-center gap-3">
           {{ platform_tag(p.name) }}
           <span class="text-sm text-[var(--text-secondary)]">{{ p["items"] | length }} 个订阅</span>
         </div>
         <svg class="w-4 h-4 text-[var(--text-secondary)] transition-transform" ...><!-- chevron --></svg>
       </div>
       <!-- Body: collapsible -->
       <div class="px-5 pb-4">
         {% if p["items"] %}
         <div class="space-y-2 mb-4">
           {% for item in p["items"] %}
           <div class="flex items-center justify-between py-2 px-3 rounded-[10px] hover:bg-gray-50 dark:hover:bg-gray-800/30 group">
             <div class="flex items-center gap-3">
               {{ status_dot("active") }}
               <div>
                 <div class="text-sm font-medium">{{ item.get("name", "-") }}</div>
                 <div class="text-xs text-[var(--text-secondary)]">{{ item.get("uid") or item.get("user_id", "-") }}</div>
               </div>
             </div>
             <form hx-post="/subscriptions/remove" hx-target="body" hx-confirm="确定删除此订阅？"
                   onsubmit="event.preventDefault(); if(confirm('确定删除此订阅？')){ htmx.trigger(this, 'submit'); }">
               <input type="hidden" name="platform" value="{{ p.key }}">
               <input type="hidden" name="identifier" value="{{ item.get('uid') or item.get('user_id', '') }}">
               <button type="submit" class="text-sm text-red-500 hover:text-red-700 opacity-0 group-hover:opacity-100 transition-opacity">删除</button>
             </form>
           </div>
           {% endfor %}
         </div>
         {% else %}<p class="text-sm text-[var(--text-secondary)] py-2">暂无订阅</p>{% endif %}

         <!-- Search section -->
         <div class="mt-4 pt-4 border-t border-gray-100 dark:border-gray-800">
           <div class="flex gap-2">
              <input type="text" name="name" placeholder="按名称搜索…"
               class="flex-1 px-3 py-2 rounded-[8px] border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm"
               hx-post="/subscriptions/search" hx-trigger="keyup changed delay:300ms"
               hx-target="#search-{{ p.key }}" hx-swap="innerHTML"
               hx-include="[name='platform']">
             <input type="hidden" name="platform" value="{{ p.key }}">
             <button class="px-3 py-2 text-sm text-[var(--text-secondary)] hover:text-apple-blue transition-colors"
               onclick="document.getElementById('manual-{{ p.key }}').classList.toggle('hidden')">手动添加</button>
           </div>
           <div id="search-{{ p.key }}" class="mt-2"></div>
           <!-- Manual add (collapsed) -->
           <div id="manual-{{ p.key }}" class="hidden mt-3 p-3 bg-gray-50 dark:bg-gray-800/30 rounded-[10px]">
             <form hx-post="/subscriptions/add" hx-target="body" hx-trigger="submit">
               <input type="hidden" name="platform" value="{{ p.key }}">
               <div class="flex gap-2">
                 <input type="text" name="identifier" placeholder="UID" required class="flex-1 px-3 py-2 rounded-[8px] border ... text-sm">
                 <input type="text" name="name" placeholder="显示名称" required class="flex-1 px-3 py-2 rounded-[8px] border ... text-sm">
                 <button type="submit" class="px-4 py-2 bg-apple-blue text-white rounded-[8px] text-sm font-medium hover:bg-blue-600 transition-colors">添加</button>
               </div>
             </form>
           </div>
         </div>
       </div>
     </div>
     ```

2. **Rewrite `_candidates.html`**:
   ```html
   {% if candidates %}
   <div class="divide-y divide-gray-100 dark:divide-gray-800 rounded-[8px] border border-gray-200 dark:border-gray-700 overflow-hidden">
     {% for c in candidates %}
     <div class="flex items-center justify-between px-3 py-2 hover:bg-blue-50 dark:hover:bg-blue-900/20 transition-colors">
       <div class="flex-1 min-w-0">
         <div class="text-sm font-medium truncate">{{ c.get("name", "?") }}</div>
         <div class="text-xs text-[var(--text-secondary)]">UID: {{ c.get("uid") or c.get("user_id", "?") }}</div>
       </div>
       <form hx-post="/subscriptions/add" hx-target="body" style="display:inline;">
         <input type="hidden" name="platform" value="{{ search_platform }}">
         <input type="hidden" name="identifier" value="{{ c.get('uid') or c.get('user_id', '') }}">
         <input type="hidden" name="name" value="{{ c.get('name', '') }}">
         <button type="submit" class="px-3 py-1 text-xs font-medium text-apple-blue hover:bg-apple-blue/10 rounded-[6px] transition-colors">+ 添加</button>
       </form>
     </div>
     {% endfor %}
   </div>
   {% else %}
   <p class="text-xs text-[var(--text-secondary)] mt-1">{{ search_msg }}</p>
   {% endif %}
   ```

3. **Add URL flash-to-toast JS** in `base.html` (or inline in subscriptions page):
   ```javascript
   (function() {
     var params = new URLSearchParams(window.location.search);
     var msg = params.get('msg');
     var type = params.get('type');
     if (msg) { showToast(msg, type || 'info'); }
     // Clean URL without reload
     if (msg) { window.history.replaceState({}, '', window.location.pathname); }
   })();
   ```

**Verification**:
- Visit `/subscriptions` — see platform group cards, collapsible
- Search for a name — candidates dropdown appears with hover effect
- Click "+ 添加" — toast shows success, page content refreshes (via HTMX `hx-target="body"` reload)
- Click "删除" on a subscription — confirm dialog, then toast + refresh
- Manual add unfold/fold works

---

## Task 6: Check Page Rewrite

**Goal**: Rewrite `web/templates/check.html` with status panel (running indicator + elapsed time + count + button), 5-segment progress bar, dark terminal log area with filter tabs (All/Error/LIVE) and auto-scroll.

**File**: `web/templates/check.html` — rewrite

**Steps**:

1. **Import**: `{% from "_macros.html" import badge, platform_tag, progress_segment %}`

2. **Status panel card**:
   ```html
   <div class="bg-[var(--card-bg)] rounded-[14px] p-5 shadow-[...] border border-[var(--card-border)] mb-6">
     <div class="flex items-center justify-between">
       <div class="flex items-center gap-4">
         <!-- Status indicator: dot + label -->
         <div id="status-indicator" class="flex items-center gap-2">
           <span id="status-dot" class="w-3 h-3 rounded-full bg-gray-400"></span>
           <span id="status-text" class="text-sm font-medium">空闲</span>
         </div>
         <!-- Stats -->
         <div class="text-sm text-[var(--text-secondary)]" id="elapsed-time">--:--</div>
         <div class="text-sm text-[var(--text-secondary)]">已处理: <span id="processed-count">0</span></div>
       </div>
       <button id="run-btn"
         class="px-5 py-2 rounded-[9px] text-sm font-medium transition-all duration-200 bg-apple-blue text-white hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
         hx-post="/check/run" hx-trigger="click">
         开始检查
       </button>
     </div>
   </div>
   ```

3. **Progress bar (5 segments)**:
   ```html
   <div class="bg-[var(--card-bg)] rounded-[14px] p-5 shadow-[...] border border-[var(--card-border)] mb-6">
     <h3 class="text-sm font-medium text-[var(--text-secondary)] mb-3">检查进度</h3>
     <div class="flex gap-1">
       {% for phase in ["discover", "download", "transcribe", "summarize", "push"] %}
       <div id="prog-{{ phase }}" class="flex-1 h-2 rounded-full bg-gray-200 dark:bg-gray-700 transition-colors duration-500"></div>
       {% endfor %}
     </div>
     <div class="flex justify-between mt-1.5">
       {% for label in ["发现", "下载", "转写", "摘要", "推送"] %}
       <span class="text-xs text-[var(--text-secondary)]">{{ label }}</span>
       {% endfor %}
     </div>
   </div>
   ```

4. **Log terminal card**:
   ```html
   <div class="bg-[#1e1e1e] rounded-[14px] shadow-[...] border border-gray-800 overflow-hidden">
     <!-- Filter bar -->
     <div class="flex items-center justify-between px-4 py-2 bg-[#252526] border-b border-gray-800">
       <div class="flex gap-1">
         <button class="filter-btn px-3 py-1 text-xs rounded-[6px] bg-blue-600 text-white" data-filter="all">全部</button>
         <button class="filter-btn px-3 py-1 text-xs rounded-[6px] text-gray-400 hover:text-white" data-filter="error">仅错误</button>
         <button class="filter-btn px-3 py-1 text-xs rounded-[6px] text-gray-400 hover:text-white" data-filter="live">LIVE</button>
       </div>
       <button onclick="document.getElementById('log-output').innerHTML = ''" class="text-xs text-gray-500 hover:text-white">清空</button>
     </div>
     <!-- Log area -->
     <div id="log-output" class="p-4 font-mono text-sm leading-relaxed h-[480px] overflow-y-auto"
       style="background: #1e1e1e; color: #d4d4d4;">
       <span class="text-gray-500">等待开始检查…</span>
     </div>
   </div>
   ```

5. **Rewrite the client-side SSE JavaScript** (replace existing `initSSE`):
   ```javascript
   /* ── Check page SSE ── */
   var evtSource = null;
   var startTime = null;
   function initSSE() {
     if (evtSource) evtSource.close();
     startTime = Date.now();
     updateStatus('running', '运行中');
     document.getElementById('run-btn').disabled = true;
     document.getElementById('run-btn').textContent = '运行中…';
     document.getElementById('run-btn').classList.remove('bg-apple-blue', 'hover:bg-blue-600');
     document.getElementById('run-btn').classList.add('bg-orange-500', 'cursor-not-allowed');
     // Reset progress bars
     document.querySelectorAll('[id^="prog-"]').forEach(function(el) { el.classList.remove('bg-green-500', 'bg-orange-500'); el.classList.add('bg-gray-200', 'dark:bg-gray-700'); });
     document.getElementById('log-output').innerHTML = '';
     document.getElementById('processed-count').textContent = '0';

     evtSource = new EventSource('/check/stream');
     evtSource.addEventListener('log', function(e) {
       var data = JSON.parse(e.data);
       appendLog(data);
       updateElapsed();
     });
     evtSource.addEventListener('done', function() {
       evtSource.close();
       evtSource = null;
       updateStatus('done', '已完成');
       document.getElementById('run-btn').disabled = false;
       document.getElementById('run-btn').textContent = '再次运行';
       document.getElementById('run-btn').classList.remove('bg-orange-500', 'cursor-not-allowed');
       document.getElementById('run-btn').classList.add('bg-green-500', 'hover:bg-green-600');
       // All segments green
       document.querySelectorAll('[id^="prog-"]').forEach(function(el) { el.classList.remove('bg-gray-200', 'dark:bg-gray-700'); el.classList.add('bg-green-500'); });
     });
   }

   function updateStatus(state, text) {
     var dot = document.getElementById('status-dot');
     dot.className = 'w-3 h-3 rounded-full ' +
       (state === 'running' ? 'bg-orange-500 animate-pulse' : state === 'done' ? 'bg-green-500' : 'bg-gray-400');
     document.getElementById('status-text').textContent = text;
   }

   function updateElapsed() {
     if (!startTime) return;
     var sec = Math.floor((Date.now() - startTime) / 1000);
     var m = Math.floor(sec / 60), s = sec % 60;
     document.getElementById('elapsed-time').textContent =
       String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
   }

   function appendLog(data) {
     var el = document.getElementById('log-output');
     // Colorize by level
     var level = data.type || 'info';
     var color = level === 'error' ? 'text-red-400' : level === 'warn' ? 'text-orange-400' : level === 'ok' ? 'text-green-400' : 'text-gray-400';
     var line = document.createElement('div');
     line.className = 'log-line';
     line.innerHTML = '<span class="text-gray-600">[' + data.time + ']</span> ' +
       '<span class="' + color + '">[' + level.toUpperCase() + ']</span> ' +
       (data.platform ? '<span class="text-blue-400">[' + data.platform + ']</span> ' : '') +
       '<span>' + escapeHtml(data.message) + '</span>';
     el.appendChild(line);
     el.scrollTop = el.scrollHeight;
   }

   function escapeHtml(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

   /* ── Log filter ── */
   document.querySelectorAll('.filter-btn').forEach(function(btn) {
     btn.addEventListener('click', function() {
       document.querySelectorAll('.filter-btn').forEach(function(b) {
         b.classList.remove('bg-blue-600', 'text-white');
         b.classList.add('text-gray-400');
       });
       this.classList.add('bg-blue-600', 'text-white');
       this.classList.remove('text-gray-400');
       var filter = this.dataset.filter;
       document.querySelectorAll('.log-line').forEach(function(line) {
         if (filter === 'all') { line.style.display = ''; }
         else if (filter === 'error') { line.style.display = line.querySelector('.text-red-400') ? '' : 'none'; }
         else if (filter === 'live') { /* show only latest 50 — implemented separately */ }
       });
     });
   });

   /* ── Wire run button ── */
   document.getElementById('run-btn').addEventListener('click', function(e) {
     e.preventDefault();
     fetch('/check/run', { method: 'POST' })
       .then(function(r) { return r.json(); })
       .then(function(data) {
         if (data.status === 'started') { initSSE(); }
         else if (data.status === 'already_running') { showToast('检查已在运行中', 'warning'); }
       });
   });
   ```

6. **Update `web/routes/check.py`** `check_run` to include `platform` field in SSE log events. Currently the `_log_callback` only sends `type`, `message`, `time`. The `run_check_once` pipeline may pass `platform` info — verify and include if available.

   ⚠️ **本任务不实现平台标签着色**（需要扩展 `_log_callback` 签名，超出本次最小改动范围，标记为后续改进）。JS 的 `if (data.platform)` 分支保留作为前向兼容——如果 SSE 事件未来带上 `.platform`，`<span class="text-blue-400">` 块会自动渲染。

**Verification**:
- Visit `/check` — status panel shows "空闲", progress bars gray
- Click "开始检查" — button changes to "运行中…" (orange), status dot pulses orange, elapsed timer ticks
- Log lines stream in with colored levels and blue platform tags
- Progress bars light up (frontend-only for now — can manually toggle by adding classes via DevTools)
- Filter "仅错误" — only error lines visible
- Resize — responsive layout holds
- Second click during run — toast warning "检查已在运行中"

---

## Task 7: Auth Page Rewrite

**Goal**: Rewrite `web/templates/login.html` with token status cards (3-column grid), QR modal overlay (semi-transparent backdrop + centered card + 4-step progress + QR image with countdown), poll-based step updates (no page reload).

**File**: `web/templates/login.html` — rewrite

**Steps**:

1. **Import**: `{% from "_macros.html" import badge, platform_tag, status_dot %}`

2. **Page header**

3. **Token status cards grid (3 columns)**:
   ```html
   <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
     {% for p in platforms %}
     <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[...] border border-[var(--card-border)]">
       <div class="flex items-center justify-between mb-4">
         <div class="flex items-center gap-2">
           {{ platform_tag(p.name) }}
           {{ status_dot("active" if "有效" in p.token_status else "error" if "过期" in p.token_status else "inactive") }}
         </div>
       </div>
       <div class="text-sm text-[var(--text-secondary)] mb-3">{{ p.token_status }}</div>
       {% if p.expires %}
       <div class="text-xs text-[var(--text-secondary)] mb-4">过期: {{ p.expires }}</div>
       {% endif %}
       <div class="flex gap-2">
         {% if "有效" in p.token_status %}
         <button class="px-3 py-1.5 text-xs rounded-[8px] border border-gray-300 dark:border-gray-600 text-[var(--text-secondary)] hover:bg-gray-50">查看</button>
         <button onclick="openQRModal('{{ p.key }}')" class="px-3 py-1.5 text-xs rounded-[8px] bg-apple-blue text-white hover:bg-blue-600">续期</button>
         {% elif "过期" in p.token_status %}
         <button onclick="openQRModal('{{ p.key }}')" class="px-3 py-1.5 text-xs rounded-[8px] bg-apple-blue text-white hover:bg-blue-600">扫码续期</button>
         {% else %}
         <button onclick="openQRModal('{{ p.key }}')" class="px-3 py-1.5 text-xs rounded-[8px] bg-apple-blue text-white hover:bg-blue-600">扫码登录</button>
         {% endif %}
       </div>
     </div>
     {% endfor %}
   </div>
   ```

4. **QR Modal (hidden by default)**:
   ```html
   <!-- Overlay -->
   <div id="qr-overlay" class="fixed inset-0 bg-black/40 backdrop-blur-sm z-50 hidden items-center justify-center" onclick="if(event.target===this)closeQRModal()">
     <!-- Modal card -->
     <div class="bg-white dark:bg-gray-800 rounded-[16px] p-6 shadow-2xl max-w-sm w-full mx-4" onclick="event.stopPropagation()">
       <!-- Close -->
       <div class="flex justify-between items-center mb-4">
         <h3 class="text-lg font-semibold" id="qr-title">扫码登录</h3>
         <button onclick="closeQRModal()" class="text-gray-400 hover:text-gray-600">&times;</button>
       </div>
       <!-- 4-step progress -->
       <div class="flex items-center gap-1 mb-6">
         <div id="step-1" class="flex-1 h-1 rounded-full bg-blue-500"></div>
         <div id="step-2" class="flex-1 h-1 rounded-full bg-gray-200 dark:bg-gray-700"></div>
         <div id="step-3" class="flex-1 h-1 rounded-full bg-gray-200 dark:bg-gray-700"></div>
         <div id="step-4" class="flex-1 h-1 rounded-full bg-gray-200 dark:bg-gray-700"></div>
       </div>
       <div class="flex justify-between text-xs text-[var(--text-secondary)] mb-6">
         <span id="step-label-1" class="text-blue-500 font-medium">二维码生成</span>
         <span id="step-label-2">等待扫描</span>
         <span id="step-label-3">确认登录</span>
         <span id="step-label-4">保存凭证</span>
       </div>
       <!-- QR image -->
       <div class="flex justify-center mb-4">
         <img id="qr-img" src="" alt="QR Code" class="w-48 h-48 rounded-[8px] border border-gray-200 dark:border-gray-700">
       </div>
       <!-- Status / countdown -->
       <div id="qr-status" class="text-center text-sm text-[var(--text-secondary)] mb-2">正在生成二维码…</div>
       <div id="qr-countdown" class="text-center text-xs text-orange-500"></div>
     </div>
   </div>
   ```

5. **Implement `openQRModal` and `closeQRModal` in JS** (inside `base.html` script):
   ```javascript
   var qrPollInterval = null;
   var qrPlatform = null;
   function openQRModal(platform) {
     qrPlatform = platform;
     document.getElementById('qr-overlay').classList.remove('hidden');
     document.getElementById('qr-overlay').classList.add('flex');
     // Reset steps
     for (var i = 1; i <= 4; i++) {
       document.getElementById('step-' + i).className = 'flex-1 h-1 rounded-full ' + (i === 1 ? 'bg-blue-500' : 'bg-gray-200 dark:bg-gray-700');
       document.getElementById('step-label-' + i).className = (i === 1 ? 'text-blue-500 font-medium' : 'text-[var(--text-secondary)]');
     }
     document.getElementById('qr-status').textContent = '正在生成二维码…';
     // Load QR image
     var img = document.getElementById('qr-img');
     img.src = '/auth/qr/' + platform + '?t=' + Date.now();
     // Start countdown (QR expires in ~120s)
     var countdown = 120;
     document.getElementById('qr-countdown').textContent = countdown + 's';
     var cdInterval = setInterval(function() {
       countdown--;
       document.getElementById('qr-countdown').textContent = countdown + 's';
       if (countdown <= 10) { document.getElementById('qr-countdown').classList.add('text-red-500'); }
       if (countdown <= 0) {
         clearInterval(cdInterval);
         document.getElementById('qr-status').textContent = '二维码已过期，请重新扫码';
         document.getElementById('qr-countdown').textContent = '';
       }
     }, 1000);

     // Update step to "等待扫描"
     document.getElementById('qr-status').textContent = '等待扫码…';
     document.getElementById('step-1').className = 'flex-1 h-1 rounded-full bg-green-500';
     document.getElementById('step-label-1').className = 'text-green-500 font-medium';
     document.getElementById('step-2').className = 'flex-1 h-1 rounded-full bg-blue-500';
     document.getElementById('step-label-2').className = 'text-blue-500 font-medium';

     // Poll
     qrPollInterval = setInterval(function() {
       fetch('/auth/poll/' + platform)
         .then(function(r) { return r.json(); })
         .then(function(data) {
           if (data.status === 'scanned') {
             document.getElementById('qr-status').textContent = '已扫码，请在手机上确认';
             document.getElementById('step-2').className = 'flex-1 h-1 rounded-full bg-green-500';
             document.getElementById('step-label-2').className = 'text-green-500 font-medium';
             document.getElementById('step-3').className = 'flex-1 h-1 rounded-full bg-blue-500';
             document.getElementById('step-label-3').className = 'text-blue-500 font-medium';
            } else if (data.status === 'confirmed') {
              document.getElementById('qr-status').textContent = '已确认，正在保存凭证…';
              document.getElementById('step-2').className = 'flex-1 h-1 rounded-full bg-green-500';
              document.getElementById('step-label-2').className = 'text-green-500 font-medium';
              document.getElementById('step-3').className = 'flex-1 h-1 rounded-full bg-green-500';
              document.getElementById('step-label-3').className = 'text-green-500 font-medium';
              document.getElementById('step-4').className = 'flex-1 h-1 rounded-full bg-blue-500';
              document.getElementById('step-label-4').className = 'text-blue-500 font-medium';
            } else if (data.status === 'success') {
             clearInterval(qrPollInterval); clearInterval(cdInterval);
             document.getElementById('qr-status').textContent = '✅ 登录成功';
             document.getElementById('step-3').className = 'flex-1 h-1 rounded-full bg-green-500';
             document.getElementById('step-label-3').className = 'text-green-500 font-medium';
             document.getElementById('step-4').className = 'flex-1 h-1 rounded-full bg-green-500';
             document.getElementById('step-label-4').className = 'text-green-500 font-medium';
             setTimeout(function() { closeQRModal(); showToast('登录成功', 'success'); location.reload(); }, 1500);
           } else if (data.status === 'waiting') {
             // Still waiting — do nothing
           } else {
             // expired / error / no_session
             clearInterval(qrPollInterval); clearInterval(cdInterval);
             document.getElementById('qr-status').textContent = data.message || '二维码已过期，请重试';
           }
         });
     }, 2000);
   }
   function closeQRModal() {
     document.getElementById('qr-overlay').classList.add('hidden');
     document.getElementById('qr-overlay').classList.remove('flex');
     if (qrPollInterval) { clearInterval(qrPollInterval); qrPollInterval = null; }
     qrPlatform = null;
   }
   ```

**Verification**:
- Visit `/auth` — 3 column cards, each with platform name + status dot + expiry + contextual buttons
- Click "扫码登录" — modal opens with semi-transparent backdrop, QR image loads, steps 1→2 active
- In DevTools, mock `data.status = 'scanned'` — steps 2→3 update
- Mock `data.status = 'success'` — step 4 turns green, toast appears, modal closes, page reloads
- Click outside modal or × — modal closes, polling stops

---

## Task 8: Settings Page Rewrite + Route Modification

**Goal**: Rewrite `web/templates/settings.html` with grouped cards, Apple toggle switches, 2-column field grid, sticky save bar with dirty detection. Modify `POST /settings` to return HTMX response (HTML fragment + HX-Trigger toast) instead of 303 redirect.

**Files**:
- `web/templates/settings.html` — rewrite
- `web/routes/settings.py` — modify `settings_save` to return HTMLResponse

**Steps**:

1. **Rewrite `settings.html`**:
   - Import: `{% from "_macros.html" import field, toggle %}`
   - **Form setup**: `<form id="settings-form" hx-post="/settings" hx-trigger="submit" hx-target="#settings-toast">` — returns a toast fragment
   - **Grouped cards**:

   **Card 1: 常规**
   ```html
   <div class="bg-[var(--card-bg)] rounded-[14px] p-5 shadow-[...] border border-[var(--card-border)] mb-4">
     <div class="flex items-center gap-2 mb-4">
       <svg class="w-5 h-5 text-apple-blue" ...><!-- gear icon --></svg>
       <h2 class="text-base font-semibold">常规</h2>
     </div>
     <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
       {{ field("data_dir", config.general.data_dir, "数据目录", width="full") }}
       <div class="flex items-center gap-3 pt-2">
         {{ toggle("disable_ssl_verify", config.general.disable_ssl_verify, "禁用 SSL 验证") }}
       </div>
     </div>
   </div>
   ```

    **Card 2: 通知**
   ```html
   <div class="bg-[var(--card-bg)] rounded-[14px] p-5 ... mb-4">
     <div class="flex items-center gap-2 mb-4">
       <svg class="w-5 h-5 text-apple-blue" ...><!-- bell icon --></svg>
       <h2 class="text-base font-semibold">通知</h2>
     </div>
     <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
       {{ field("gotify_url", config.bilibili.notification.gotify_url, "Gotify URL", width="full") }}
       {{ field("gotify_token_bili", config.bilibili.notification.gotify_token, "Gotify Token (B站)", type="password") }}
       {{ field("gotify_token_xhs", config.xiaohongshu.notification.gotify_token, "Gotify Token (小红书)", type="password") }}
       {{ field("gotify_token_weibo", config.weibo.notification.gotify_token, "Gotify Token (微博)", type="password") }}
     </div>
   </div>
   ```

    **Card 3: 平台启用**
   ```html
   <div class="bg-[var(--card-bg)] rounded-[14px] p-5 ... mb-4">
     <div class="flex items-center gap-2 mb-4">...</div>
     <div class="flex flex-col gap-3">
       <div class="flex items-center justify-between">
         <div><div class="text-sm font-medium">小红书</div><div class="text-xs text-[var(--text-secondary)]">抓取小红书内容</div></div>
         {{ toggle("xhs_enabled", config.xiaohongshu.enabled) }}
       </div>
       <div class="flex items-center justify-between">
         <div><div class="text-sm font-medium">微博</div><div class="text-xs text-[var(--text-secondary)]">抓取微博内容</div></div>
         {{ toggle("weibo_enabled", config.weibo.enabled) }}
       </div>
       <div class="text-xs text-[var(--text-secondary)]">B站始终启用</div>
     </div>
   </div>
   ```

   **Sticky save bar**:
   ```html
   <div id="save-bar" class="fixed bottom-0 left-60 right-0 md:left-60 bg-white/90 dark:bg-gray-900/90 backdrop-blur-[12px] border-t border-gray-200 dark:border-gray-800 px-6 py-3 z-30 hidden transition-all">
     <div class="flex items-center justify-between max-w-4xl mx-auto">
       <span class="text-sm text-orange-500 font-medium">有未保存的更改</span>
       <div class="flex gap-2">
         <button type="button" onclick="resetSettings()" class="px-4 py-2 text-sm rounded-[9px] border border-gray-300 text-[var(--text-secondary)] hover:bg-gray-50">放弃</button>
         <button type="submit" form="settings-form" class="px-4 py-2 text-sm rounded-[9px] bg-apple-blue text-white hover:bg-blue-600 font-medium">保存</button>
       </div>
     </div>
   </div>
   ```

   **Toast target**: `<div id="settings-toast"></div>` at bottom of form (invisible, used for HTMX response swap)

   **Dirty detection JS**:
   ```javascript
   (function() {
     var form = document.getElementById('settings-form');
     var saveBar = document.getElementById('save-bar');
     var initialData = new FormData(form);
     form.addEventListener('change', checkDirty);
     form.addEventListener('input', checkDirty);
     function checkDirty() {
       var current = new FormData(form);
       var dirty = false;
       for (var key of initialData.keys()) {
         if (initialData.get(key) !== current.get(key)) { dirty = true; break; }
       }
       saveBar.style.display = dirty ? 'block' : 'none';
     }
     window.resetSettings = function() { location.reload(); };
   })();
   ```

2. **Modify `web/routes/settings.py`** `settings_save`:
   - Change return type from `RedirectResponse` to `HTMLResponse`
   - Instead of `return RedirectResponse(url="/settings", status_code=303)`, return:
     ```python
     from fastapi.responses import HTMLResponse
     # After successful save:
     headers = {"HX-Trigger": '{"toast":{"msg":"设置已保存","type":"success"}}'}
     return HTMLResponse(content="", headers=headers, status_code=200)
     ```
   - Update the function signature and imports accordingly
   - **Note**: `Form(False)` on bool fields with HTMX: HTMX unchecked toggles don't send the field. Need to handle this. The existing `Form(False)` default already handles this because if the checkbox is unchecked, it's not sent, so the default `False` is used. For toggle switches (hidden input approach), verify that unchecked toggles still send `False`. One approach: add a hidden input before each toggle: `<input type="hidden" name="{{ name }}" value="false">` and let the toggle checkbox override it with `"true"` when checked. Implement this in the `toggle` macro.

3. **Verify form field name alignment**: toggle macro 已保证提交 `true`/`false` 字符串；FastAPI `Form(False)` 接受 bool 字段时正确解析 `'true'`→True、`'false'`→False。无需改路由签名。

**Verification**:
- Visit `/settings` — see 3 grouped cards (常规 / 通知 / 平台启用) with icons, fields in 2-column grid, toggles styled as Apple switches
- Change a value — sticky bottom bar appears with "有未保存的更改" + [放弃] [保存]
- Click "保存" — toast "设置已保存" appears, save bar disappears
- Click "放弃" — page reloads, changes discarded
- Test with `curl -X POST http://localhost:8080/settings -d "data_dir=./test"` — returns 200 with `HX-Trigger` header (not 303)
- Toggle switches work correctly: checked → `true`, unchecked → `false`
- `uv run pytest tests/test_web_settings.py` — update test `test_settings_save` to assert 200 instead of 303

---

## Task 9: Final Verification + Test Updates

**Goal**: Ensure all tests pass, update tests where HTML structure / route behavior changed, manual verification across 5 pages in light/dark/mobile.

**Files**:
- `tests/test_web_settings.py` — update `test_settings_save` assertion
- `tests/test_web_*.py` — review all assertions for HTML text content that may have changed
- Any other test files affected

**Steps**:

1. **Update `tests/test_web_settings.py`**:
   - `test_settings_save`: change `assert resp.status_code == 303` to `assert resp.status_code == 200`
   - Change `assert resp.headers["location"] == "/settings"` to `assert "HX-Trigger" in resp.headers` (verify the HX-Trigger header exists)
   - Add a test for the toast trigger: `assert "toast" in resp.headers.get("HX-Trigger", "")`

2. **Review all other web tests for HTML content assertions**:
    - `test_web_subscriptions.py`: `test_search_returns_html` asserts `"UP主" in resp.text` — this should still work since candidates content has name. `test_search_empty` asserts `"未找到" in resp.text` — **无需改动**（empty state 文本由后端 mock 控制，`_candidates.html` 渲染 `{{ search_msg }}`，与模板改动无关）。
   - `test_web_dashboard.py`: only checks status 200 and content-type — no text assertions, safe
   - `test_web_check.py`: only checks status codes, SSE content, JSON — all backend, safe
   - `test_web_auth.py`: only checks status codes — safe

3. **Run test suite**:
   ```bash
   uv run ruff check .
   uv run pyright .
   uv run pytest -x
   ```

4. **Manual verification checklist** (on `http://localhost:8080`):
   - [ ] Dashboard: stat cards visible, phase badges colored, timeago formatting
   - [ ] Dashboard: toggle dark mode in DevTools → all colors adapt
   - [ ] Dashboard: resize to <768px → hamburger menu works
   - [ ] Subscriptions: platform group cards, collapsible
   - [ ] Subscriptions: search → candidates appear with hover
   - [ ] Subscriptions: add subscription (search candidate or manual) → toast success
   - [ ] Subscriptions: delete → confirm dialog → toast + refresh
   - [ ] Check: status panel shows idle, click start → running state, SSE logs stream with colors
   - [ ] Check: log filter tabs switch view
   - [ ] Check: progress bars update (manual class toggle in DevTools to verify styling)
   - [ ] Auth: 3-column cards, contextual buttons
   - [ ] Auth: open QR modal → overlay + steps + countdown
   - [ ] Auth: mock poll responses in DevTools → step progression
   - [ ] Settings: grouped cards, toggle switches, sticky save bar with dirty detection
   - [ ] Settings: save → toast + no redirect
   - [ ] Settings: "放弃" → page reload
   - [ ] All pages: emoji-free sidebar icons, consistent Apple-style look
   - [ ] All pages: `prefers-color-scheme: dark` in DevTools → dark mode consistent

5. **Fix any issues found**: Iterate on failing tests or visual regressions.

**Verification**:
- `uv run ruff check .` — no new lint issues
- `uv run pyright .` — no new type errors
- `uv run pytest -x` — all tests green
- Manual walkthrough confirms 5 pages in light/dark/mobile all work

---

## Summary of Backend Changes

These are the **minimal** backend changes required to support the new frontend:

| File | Change | Reason |
|---|---|---|
| `web/app.py` | Add `timeago` + `phase_color` Jinja filters | New template needs relative time formatting |
| `web/routes/dashboard.py` | Pass `last_updated` to template context | Dashboard shows "上次更新 X 分钟前" |
| `web/routes/settings.py` | `settings_save` returns `HTMLResponse(200)` + `HX-Trigger` instead of `303 Redirect` | HTMX submission needs toast feedback, no page reload |
| `web/static/app.css` | **Delete** | Replaced by `tokens.css` + Tailwind |
| `web/static/tokens.css` | **Create** | CSS variables + animations |
| `web/templates/base.html` | **Rewrite** | Tailwind + Apple tokens + sidebar + dark mode + hamburger |
| `web/templates/_macros.html` | **Create** | Component library |
| `web/templates/dashboard.html` | **Rewrite** | New Apple-style layout |
| `web/templates/subscriptions.html` | **Rewrite** | Grouped cards + search + HTMX |
| `web/templates/_candidates.html` | **Rewrite** | Styled search results |
| `web/templates/check.html` | **Rewrite** | Status panel + progress + terminal |
| `web/templates/login.html` | **Rewrite** | Card grid + QR modal |
| `web/templates/settings.html` | **Rewrite** | Grouped cards + toggles + sticky save |
| `tests/test_web_settings.py` | Update assertions | Settings route no longer returns 303 |

**Routes that remain completely untouched**: `GET /subscriptions`, `POST /subscriptions/add`, `POST /subscriptions/remove`, `POST /subscriptions/search`, `GET /check`, `POST /check/run`, `GET /check/stream`, `GET /auth`, `GET /auth/qr/{key}`, `GET /auth/poll/{key}`, `GET /settings` (GET only).
