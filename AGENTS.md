# Trawler Project Rules

Python 3.12 trawler project. Overrides global rules where they conflict.

## Commands

```bash
uv venv --python 3.12                             # 创建虚拟环境（首次）
uv pip install -e ".[dev]"                        # 安装依赖（含 dev，不含 xhs 和 transcribe）
uv pip install -e ".[transcribe]"                 # 额外安装语音转写依赖（可选）
uv run ruff check .                              # lint
uv run ruff format .                             # format
uv run pyright                                     # type check（无参数！见全局 AGENTS.md）
uv run pytest -x                                 # test (fail fast)
uv run trawler check --platform all              # run locally
```

**所有 Python 环境操作必须通过 `uv`，禁止直接使用 `python3`、`pip`、`pip3` 等裸命令。**

## Architecture

```
core/            流程编排, 纯编排层
shared/          共享模块: config, protocols(dataclass), downloader, http
platforms/       平台适配层: bilibili/, xiaohongshu/
```

- **数据模型**全部在 `shared/protocols.py`，`dataclass` 定义，`Protocol` 定义行为契约
- **配置**在 `shared/config.py`，`dataclass` 驱动，环境变量覆盖
- **平台模块模式**：`auth.py` + `monitor.py` + `comments.py` [+ `downloader.py` / `parser.py`]
- CLI 入口 `run_check.py`，Click 命令 `trawler`

## Conventions

- `from __future__ import annotations` 在所有模块文件顶部
- 函数签名必须有 type hint，参数必须标类型，leaf 函数返回值可省略
- Rich console：用户可见输出用 `console.print(...)`，调试用 `logging.getLogger(__name__)`
- Console emoji 前缀：[cyan]🔍[/] [green]✓[/] [yellow]⚠️[/] [red]✗[/] [bold blue]▶[/] [dim]⬇ 📝 💬 🤖[/dim]
- Section 注释用 Unicode box-drawing：`# ═══════════════` / `# ── ──`
- import 顺序：标准库 → 第三方 → 本地，用 ruff I 维护
- 新增平台必须：`platforms/<name>/__init__.py`（docstring only）+ `auth.py` + `monitor.py` + `comments.py`
- json 持久化用 `shared/message_store.MessageStore` (mark_phase/mark_error 仅内存, save 才写盘)
- 异步：`async def` + `await`，CLI 入口 `asyncio.run()`

## Common Tasks

### 新增一个平台
1. 建 `platforms/<name>/` 目录 + `__init__.py`（docstring only）
2. 写数据模型到 `shared/protocols.py`
3. 写 `auth.py`（认证获取）、`monitor.py`（内容检查）、`comments.py`（评论）
4. pipeline.py 加新平台分支
5. `shared/config.py` 加平台 dataclass
6. `run_check.py` 加 CLI option

### 修改配置结构
- 改 `shared/config.py` 的 dataclass
- 保持 `_apply_env_overrides()` 同步更新
- `config.yaml.example` 同步更新

## Gotchas

- 不要改 `print()` / `console.print()` 的 emoji 和颜色标签，它们是外部接口的一部分
- 不要改已有 error message 的文本，CI 和日志分析可能依赖它们
- 新增依赖：必须确认是否真需要，标准库优先
- `asyncio.run()` 是唯一入口模式，不用 `loop.run_until_complete()`
