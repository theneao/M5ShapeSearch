#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
完整启动脚本：同时启动 FastAPI 服务和 Gradio 演示 UI

修改时间：2026-08-27
修改作用：启用父进程和子进程无缓冲输出，启动提示及服务日志默认实时显示在终端。

修改时间：2026-08-28
修改作用：API 默认监听 0.0.0.0，并在终端显示手表可访问的局域网地址。

修改时间：2026-08-28
修改作用：固定并校验 8000/7860/7861 端口，同时启动手绘页和整体设置页，防止固件误连备用端口。

修改时间：2026-08-29
修改作用：API 端口保持固定；Gradio 端口被旧页面占用时自动选择后续空闲端口并打印实际 URL，
          启动成功提示前先校验三个子进程仍在运行。

修改时间：2026-08-29
修改作用：识别市场数据结构化进度事件，在终端原位刷新单行进度条；过滤逐请求/逐标的成功日志。

修改时间：2026-08-30
修改作用：项目统一更名为 M5ShapeSearch，并同步启动横幅。

修改时间：2026-08-31
修改作用：将手绘、数据设置和运行看板合并到唯一的 7860 Web UI，删除 7861 设置子进程；
          启动、存活检查和退出清理均只管理 FastAPI 与统一 UI。

使用方式：
    python start_all.py
"""
import os
import subprocess
import time
import sys
import socket
import threading


_output_lock = threading.Lock()
_terminal_progress_line = ""
_terminal_progress_owner = ""


def _render_progress(name, payload):
    """把 API 子进程的 fraction|message 事件渲染成不滚屏的终端进度条。"""
    global _terminal_progress_line, _terminal_progress_owner
    try:
        fraction_text, message = payload.split("|", 1)
        fraction = min(1.0, max(0.0, float(fraction_text)))
    except (ValueError, TypeError):
        return
    width = 36
    completed = min(width, int(round(fraction * width)))
    bar = "#" * completed + "-" * (width - completed)
    rendered = f"[{name}] 数据加载 [{bar}] {fraction * 100:5.1f}%  {message[:68]}"
    with _output_lock:
        _terminal_progress_line = rendered
        _terminal_progress_owner = name
        sys.stdout.write("\r" + rendered.ljust(150))
        if fraction >= 0.9999:
            sys.stdout.write("\n")
            _terminal_progress_line = ""
            _terminal_progress_owner = ""
        sys.stdout.flush()


def _print_log_line(name, line, finish_progress=False):
    """普通日志临时让出当前进度行，输出后按需恢复。"""
    global _terminal_progress_line, _terminal_progress_owner
    with _output_lock:
        active = _terminal_progress_line
        if active:
            sys.stdout.write("\r" + " " * 150 + "\r")
        if finish_progress:
            _terminal_progress_line = ""
            _terminal_progress_owner = ""
        print(f"[{name}] {line}", flush=True)
        if _terminal_progress_line:
            sys.stdout.write("\r" + _terminal_progress_line.ljust(150))
            sys.stdout.flush()

# 修复 Windows 编码，并强制逐行实时输出，避免日志在程序退出时才一次性显示。
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )


def read_logs(proc, name):
    """实时读取子进程日志；高频成功详情折叠为进度条。"""
    if proc.stdout is None:
        return
    try:
        for line in iter(proc.stdout.readline, ""):
            if line:
                text = line.rstrip()
                marker = "[DATA][PROGRESS]"
                if marker in text:
                    _render_progress(name, text.split(marker, 1)[1].strip())
                    continue
                if any(item in text for item in (
                    "[BINANCE][HTTP]", "[BINANCE][KLINE][OK]", "[DATA][OK]"
                )):
                    continue
                finish = "[REFRESH][DONE]" in text or "[REFRESH][ERROR]" in text
                _print_log_line(name, text, finish_progress=finish)
    except (OSError, ValueError):
        pass

def port_available(port):
    """通过独占 bind+listen 检查端口，避免 Windows 上只 bind 未监听造成误判。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("0.0.0.0", port))
        sock.listen(1)
        sock.close()
        return True
    except OSError:
        return False


def choose_web_port(preferred, reserved):
    """Gradio 仅供浏览器使用，可在首选端口冲突时顺延，最多尝试 20 个端口。"""
    for port in range(preferred, preferred + 20):
        if port not in reserved and port_available(port):
            return port
    return None


def get_lan_ip():
    """尽量获取同一局域网内硬件可访问的 IPv4 地址。"""
    # Windows 上 VPN/TUN 常把 UDP 默认路由指向 198.18.0.0/15；
    # 优先选择真实家庭局域网常用的 192.168.x.x。
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        preferred = [item for item in addresses if item.startswith("192.168.")]
        if preferred:
            return preferred[0]
        private = [item for item in addresses if item.startswith("10.") or item.startswith("172.")]
        if private:
            return private[0]
    except OSError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()

def main():
    print("\n" + "="*80)
    print("[INFO] M5ShapeSearch Service - Complete Startup")
    print("="*80 + "\n")

    api_port = int(os.environ.get("API_PORT", "8000"))
    requested_ui_port = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
    if not port_available(api_port):
        print(f"[ERROR] API 端口 {api_port} 已被占用。", flush=True)
        print("[HINT] 硬件固定连接 API 端口，请先停止旧的 FastAPI/start_all.py 再重试。", flush=True)
        return 2
    ui_port = choose_web_port(requested_ui_port, {api_port})
    if ui_port is None:
        print("[ERROR] 无法为 Gradio 页面找到空闲端口。", flush=True)
        return 2
    if ui_port != requested_ui_port:
        print(f"[WARN] UI 端口 {requested_ui_port} 已占用，改用 {ui_port}。", flush=True)
    lan_ip = get_lan_ip()

    print(f"[INFO] API 服务端口: {api_port}")
    print(f"[INFO] UI 服务端口: {ui_port}\n")

    # 父、子进程都使用 UTF-8 无缓冲输出，确保日志立即进入当前终端。
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["GRADIO_SERVER_PORT"] = str(ui_port)
    env["API_PORT"] = str(api_port)
    env["SHAPE_API_BASE"] = f"http://127.0.0.1:{api_port}"

    # 启动 FastAPI
    print("[STEP 1] 启动 FastAPI 服务...")
    api_proc = subprocess.Popen(
        [
            sys.executable, "-u", "-m", "uvicorn",
            "main:app",
            "--host", "0.0.0.0",
            "--port", str(api_port),
            "--log-level", "info"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    print(f"[OK] FastAPI 进程已启动 (PID: {api_proc.pid})")
    t_api = threading.Thread(target=read_logs, args=(api_proc, "API"), daemon=True)
    t_api.start()
    time.sleep(3)  # 等待服务启动
    if api_proc.poll() is not None:
        print(f"[ERROR] FastAPI 启动失败，退出码 {api_proc.returncode}。", flush=True)
        return 3

    # 启动 Gradio
    print("\n[STEP 2] 启动 Gradio 统一 UI...")
    ui_proc = subprocess.Popen(
        [sys.executable, "-u", "server_web_ui.py", "--port", str(ui_port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    print(f"[OK] Gradio 进程已启动 (PID: {ui_proc.pid})")
    t_ui = threading.Thread(target=read_logs, args=(ui_proc, "UI"), daemon=True)
    t_ui.start()
    time.sleep(5)

    failed = [(name, proc.returncode) for name, proc in (
        ("API", api_proc), ("UI", ui_proc)
    ) if proc.poll() is not None]
    if failed:
        print(f"[ERROR] 页面启动校验失败: {failed}", flush=True)
        for proc in (api_proc, ui_proc):
            if proc.poll() is None:
                proc.terminate()
        return 3

    print("\n" + "="*80)
    print("OK - All services started!")
    print("="*80)
    print(f"\nAPI Service:  http://127.0.0.1:{api_port}")
    print(f"   • Hardware/LAN: http://{lan_ip}:{api_port}")
    print(f"   • Health check: http://127.0.0.1:{api_port}/api/v1/health")
    print(f"   • API docs: http://127.0.0.1:{api_port}/docs")
    print(f"\nUnified UI: http://127.0.0.1:{ui_port}")
    print("   • 手绘匹配 / 整体设置 / 数据刷新 / 运行状态")
    print("\nWorkflow:")
    print(f"   1. Open :{ui_port} to configure preload/cache or draw a shape")
    print("   2. Wait for Runtime Status to become ready")
    print(f"   3. Draw a shape in :{ui_port} or on M5StopWatch")
    print("   4. Matching only searches the preloaded memory cache\n")

    print("[INFO] Press Ctrl+C to stop\n")
    print("="*80)
    print("LIVE LOGS:")
    print("="*80 + "\n")

    try:
        while all(proc.poll() is None for proc in (api_proc, ui_proc)):
            time.sleep(0.5)
        exited = [(name, proc.returncode) for name, proc in (
            ("API", api_proc), ("UI", ui_proc)
        ) if proc.poll() is not None]
        print(f"[ERROR] 子进程意外退出: {exited}", flush=True)

    except KeyboardInterrupt:
        print("\n\nShutting down...")
        for proc in (api_proc, ui_proc):
            if proc.poll() is None:
                proc.terminate()
        time.sleep(1)
        for proc in (api_proc, ui_proc):
            if proc.poll() is None:
                proc.kill()
        print("Services stopped")
    finally:
        for proc in (api_proc, ui_proc):
            if proc.poll() is None:
                proc.terminate()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
