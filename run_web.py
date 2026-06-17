#!/usr/bin/env python3
"""Web UI entry point.

Usage examples::

    # 前台运行（默认）
    uv run python run_web.py --port 8080

    # 开发模式（自动 reload）
    uv run python run_web.py --reload

    # 后台运行（daemon）
    uv run python run_web.py --daemon --port 8080
    # 输出: Started in background, PID=12345, log: /tmp/trawler-web.log

    # 停止后台进程
    uv run python run_web.py --stop
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from typing import IO, NoReturn

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_PID_FILE = "/tmp/trawler-web.pid"
DEFAULT_LOG_FILE = "/tmp/trawler-web.log"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trawler Web UI entry point.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8080"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="开发模式：文件变更自动重启（与 --daemon 不兼容）",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="后台运行（POSIX double-fork），不占用 shell",
    )
    parser.add_argument(
        "--pid-file",
        default=DEFAULT_PID_FILE,
        help=f"PID 文件路径，daemon 模式下使用，默认 {DEFAULT_PID_FILE}",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"日志输出路径，daemon 模式下使用，默认 {DEFAULT_LOG_FILE}",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="停止 daemon 进程（读取 --pid-file 杀掉进程）",
    )
    return parser.parse_args(argv)


def _stop_daemon(pid_file: str) -> NoReturn:
    """读取 pid 文件并杀掉后台进程。"""
    try:
        with open(pid_file, encoding="utf-8") as fp:
            pid_str = fp.read().strip()
    except FileNotFoundError:
        print(f"No pid file at {pid_file}, nothing to stop.", file=sys.stderr)
        sys.exit(1)

    if not pid_str:
        print(f"Pid file {pid_file} is empty.", file=sys.stderr)
        sys.exit(1)

    try:
        pid = int(pid_str)
    except ValueError:
        print(f"Invalid pid in {pid_file}: {pid_str!r}", file=sys.stderr)
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"Process {pid} not running.", file=sys.stderr)
        _cleanup_pid_file(pid_file)
        sys.exit(1)

    _cleanup_pid_file(pid_file)
    print(f"Sent SIGTERM to pid={pid}.")
    sys.exit(0)


def _cleanup_pid_file(pid_file: str) -> None:
    """删除 pid 文件，忽略不存在错误。"""
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass


def _daemonize(log_file: str, pid_file: str) -> None:
    """POSIX double-fork 脱离控制终端，孙进程继续运行。

    通过 pipe 把最终 daemon PID 回传给原始父进程，用于提示用户。
    """
    if not hasattr(os, "fork"):
        raise NotImplementedError("daemon mode is only supported on POSIX systems")

    # pipe 用于 daemon 向原始父进程回报 PID
    read_fd, write_fd = os.pipe()

    # 保存启动时的 cwd（通常是项目根目录），daemon 不能 chdir("/") 否则
    # load_config() 的相对路径 config/config.toml 会找不到。
    project_cwd = os.getcwd()

    # ── 第一次 fork：父进程退出，子进程成为孤儿
    first_pid = os.fork()
    if first_pid > 0:
        # 父进程：关闭写端，读 PID，等子进程退出后退出
        os.close(write_fd)
        with os.fdopen(read_fd, "r", encoding="utf-8") as read_pipe:
            line = read_pipe.readline()
        if line:
            daemon_pid = line.strip()
            print(
                f"Started in background, PID={daemon_pid}, log: {log_file}",
                flush=True,
            )
        # 子进程已 daemon 化，无需 wait；它由 init 接管
        sys.exit(0)

    # ── 中间进程（孤儿）：脱离原 controlling terminal，成为 session leader
    os.setsid()

    # ── 第二次 fork：确保后续无法重新获取 tty
    second_pid = os.fork()
    if second_pid > 0:
        # 中间进程：直接退出，让孙进程被 init 接管
        os._exit(0)  # noqa: S300 - daemon 中间进程，避免执行 atexit 钩子

    # ── 孙进程：最终的 daemon
    os.close(read_fd)

    os.umask(0)
    # 保持项目根目录作为 cwd，让相对路径 config/config.toml 可用。
    # 传统 daemon 会 chdir("/") 避免占用 mount point，但本项目配置依赖
    # 相对路径，且项目目录通常不是可卸载的文件系统。
    os.chdir(project_cwd)

    # 重定向 stdio 到 log 文件（避免占用原终端）
    log_fd = os.open(log_file, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, sys.stdin.fileno())
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    # 写 PID 文件
    current_pid = os.getpid()
    with open(pid_file, "w", encoding="utf-8") as fp:
        fp.write(f"{current_pid}\n")

    # 把 PID 回报给原始父进程，然后关闭 pipe
    write_pipe: IO[str] = os.fdopen(write_fd, "w", encoding="utf-8")
    write_pipe.write(f"{current_pid}\n")
    write_pipe.flush()
    write_pipe.close()


def _run_uvicorn(host: str, port: int, reload: bool) -> None:
    """启动 uvicorn，单 worker：SSE 队列和检查任务在进程内存中。"""
    uvicorn.run("web.app:app", host=host, port=port, reload=reload)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.stop:
        _stop_daemon(args.pid_file)

    if args.daemon and args.reload:
        print("error: --daemon and --reload are mutually exclusive", file=sys.stderr)
        sys.exit(2)

    if args.daemon:
        _daemonize(args.log_file, args.pid_file)

    _run_uvicorn(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
