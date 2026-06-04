# -*- coding: utf-8 -*-
"""
跨平台工具模块
==============
封装 macOS / Windows / Linux 的进程管理和系统差异，
让上层业务代码无需关心平台细节。

使用:
    from platform_utils import (create_process_group, kill_process_group,
                                 install_hint, IS_WINDOWS, IS_MACOS)
"""

import os
import sys
import shutil
import subprocess
import signal

# ---- 平台标识 ----
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ============================================================
# 进程组管理（解决 macOS preexec_fn 和 os.killpg 不可跨平台的问题）
# ============================================================

def create_process_group():
    """返回 subprocess.Popen() 中创建新进程组的跨平台参数

    用法:
        kwargs = create_process_group()
        proc = subprocess.Popen(["command"], **kwargs)

    返回:
        dict: 可直接 ** 展开传给 Popen
    """
    if IS_WINDOWS:
        # Windows: CREATE_NEW_PROCESS_GROUP 让子进程进入独立进程组
        # 常量值 = 0x00000200
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        # macOS / Linux: start_new_session 内部已调用 setsid()，创建新会话（等同于进程组）
        return {"start_new_session": True}


def kill_process_group(proc: subprocess.Popen):
    """跨平台终止整个进程组（含所有子进程）

    用法:
        proc = subprocess.Popen(["command"], **create_process_group())
        # ... 需要强制终止时 ...
        kill_process_group(proc)

    参数:
        proc: 使用 create_process_group() 创建的 Popen 对象
    """
    if proc is None:
        return

    if IS_WINDOWS:
        # Windows: TerminateProcess 终止根进程，子进程由系统回收
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = int(proc._handle) if proc._handle else 0
        if handle:
            # 1 = exit code，可任意
            kernel32.TerminateProcess(handle, 1)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        # macOS / Linux: 向整个进程组发 SIGTERM，失败则 SIGKILL
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=2)
            except Exception:
                pass


def safe_terminate(proc: subprocess.Popen, timeout: float = 5.0):
    """优雅终止进程：先 SIGTERM，超时后强制 SIGKILL

    这是 kill_process_group 的温和版本，优先给进程清理机会。

    参数:
        proc: Popen 对象
        timeout: 等待超时（秒）
    """
    if proc is None or proc.poll() is not None:
        return

    if IS_WINDOWS:
        kill_process_group(proc)
    else:
        try:
            proc.terminate()  # 只发主进程 SIGTERM
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)


# ============================================================
# 命令行工具检测 & 安装提示
# ============================================================

def get_dependency_check_cmd():
    """返回平台特定的命令检测方式

    返回:
        (检测命令名, 命令行参数列表)
    """
    if IS_WINDOWS:
        return ("where", ["where", "idevice_id"])
    else:
        return ("which", ["which", "idevice_id"])


def install_hint(tool_name: str = "libimobiledevice") -> str:
    """返回跨平台的依赖安装指南

    参数:
        tool_name: 工具名称，用于生成针对性提示

    返回:
        str: 适合当前平台的安装说明
    """
    if IS_WINDOWS:
        return (
            "未找到 idevice_id 命令。\n"
            "请安装 libimobiledevice for Windows：\n"
            "  方式 1（推荐）: 从 GitHub Releases 下载并安装\n"
            "    https://github.com/libimobiledevice-win32/imobiledevice-net/releases\n"
            "  方式 2: 使用 winget 安装\n"
            "    winget install libimobiledevice\n"
            "\n"
            "安装后请重启终端或刷新 PATH 环境变量。"
        )
    elif IS_MACOS:
        return (
            "未找到 idevice_id 命令。\n"
            "请先安装: brew install libimobiledevice usbmuxd"
        )
    else:  # Linux
        return (
            "未找到 idevice_id 命令。\n"
            "请先安装:\n"
            "  Ubuntu/Debian: sudo apt install libimobiledevice-utils usbmuxd\n"
            "  Fedora:        sudo dnf install libimobiledevice usbmuxd\n"
            "  Arch:          sudo pacman -S libimobiledevice usbmuxd"
        )


def check_dependencies() -> bool:
    """检查核心依赖（idevice_id）是否可用

    返回:
        bool: 依赖已安装为 True
    """
    try:
        cmd_name, cmd_args = get_dependency_check_cmd()
        result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=10)
        return result.returncode == 0 and result.stdout.strip() != ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ============================================================
# pip 命令检测 & yt-dlp 安装提示
# ============================================================

def get_pip_command() -> str:
    """返回当前平台可用的 pip 命令

    优先级:
        macOS/Linux:  pip3 → pip → python3 -m pip
        Windows:      pip → pip3 → py -m pip

    返回:
        str: 可用的 pip 可执行文件路径（或名称），失败返回 "pip"
    """
    if IS_WINDOWS:
        # Windows 优先 pip，其次 py -m pip
        candidates = ["pip", "pip3"]
        for cmd in candidates:
            if shutil.which(cmd):
                return cmd
        # 最后兜底：尝试 py -m pip
        try:
            result = subprocess.run(
                ["py", "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return "py -m pip"
        except Exception:
            pass
        return "pip"
    else:
        # macOS / Linux 优先 pip3
        candidates = ["pip3", "pip"]
        for cmd in candidates:
            if shutil.which(cmd):
                return cmd
        # 兜底：python3 -m pip
        try:
            result = subprocess.run(
                ["python3", "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return "python3 -m pip"
        except Exception:
            pass
        return "pip3"


def get_yt_dlp_install_hint() -> str:
    """生成跨平台的 yt-dlp 安装指南（面向最终用户）

    返回:
        str: 多行安装说明文本
    """
    pip_cmd = get_pip_command()

    lines = [
        "下载失败: 未安装 yt-dlp",
        "",
        "yt-dlp 是用于下载视频的必需工具，需要先安装。",
        "",
        "请在终端中执行以下命令：",
        f"  {pip_cmd} install yt-dlp",
        "",
    ]

    if IS_MACOS:
        lines.append("💡 提示: 如果遇到权限问题，可以尝试：")
        lines.append(f"  {pip_cmd} install --user yt-dlp")
        lines.append("  或使用 Homebrew: brew install yt-dlp")
    elif IS_WINDOWS:
        lines.append("💡 提示: 如果遇到权限问题，可以尝试：")
        lines.append(f"  {pip_cmd} install --user yt-dlp")
        lines.append("  或以管理员身份运行终端后再执行安装命令")
    else:  # Linux
        lines.append("💡 提示: 如果遇到权限问题，可以尝试：")
        lines.append(f"  {pip_cmd} install --user yt-dlp")
        lines.append("  或使用系统包管理器安装（如 apt install yt-dlp）")

    return "\n".join(lines)


def check_yt_dlp_available() -> bool:
    """检查 yt-dlp 命令行工具是否可用

    返回:
        bool: yt-dlp 已安装为 True
    """
    return bool(shutil.which("yt-dlp") or shutil.which("ytdlp"))


# ============================================================
# 路径 & 文件系统
# ============================================================

def get_temp_result_file(prefix: str = "tk_chooser") -> str:
    """返回跨平台的临时结果文件路径

    用于 choose_dir_tool.py 的结果传递。
    macOS: /tmp/tk_chooser_<timestamp>.json
    Windows: %TEMP%\tk_chooser_<timestamp>.json

    参数:
        prefix: 文件名前缀

    返回:
        str: 临时文件的完整路径
    """
    import tempfile
    import time

    temp_dir = tempfile.gettempdir()
    # 使用时间戳避免碰撞
    return os.path.join(temp_dir, f"{prefix}_{int(time.time() * 1000000)}.json")


def normalize_path(path_str: str) -> str:
    """标准化路径字符串，处理平台差异（正斜杠/反斜杠）

    参数:
        path_str: 原始路径字符串

    返回:
        str: 标准化后的路径
    """
    return os.path.normpath(path_str)


# ============================================================
# 键盘快捷键（用于 GUI 自动化的文件选择对话框）
# ============================================================

def get_folder_shortcut() -> tuple:
    """返回「跳转到文件夹」对话框的快捷键

    macOS:  Cmd + Shift + G
    Windows: Ctrl + L (地址栏) 或直接输入完整路径

    返回:
        (修饰键列表, 按键)  — 适用于 pyautogui.hotkey()
    """
    if IS_WINDOWS:
        return (["ctrl"], "l")
    else:
        return (["command", "shift"], "g")


def get_select_all_shortcut():
    """全选快捷键"""
    if IS_WINDOWS:
        return (["ctrl"], "a")
    else:
        return (["command"], "a")
