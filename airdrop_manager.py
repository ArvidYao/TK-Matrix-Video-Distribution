#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AirDrop 文案发送模块
====================

通过 macOS NSSharingService API 调起系统标准 AirDrop 分享面板，
将文案保存为 .txt 文件后发送到 iPhone。

原理：
1. 将文案内容写入临时 .txt 文件
2. 启动独立子进程 (airdrop_share_helper.py)，初始化 NSApplication 后
   调用 NSSharingService 直接弹出系统标准 AirDrop 分享面板（完全可交互）
3. 用户在 AirDrop 面板中点击目标设备
4. iPhone 端点击「接受」→ 自动打开备忘录/文件 → 复制文案

为什么用独立子进程：
- NSSharingService 需要 NSApplication 运行循环来维持分享窗口
- 与 Flask 主进程分离，避免运行循环冲突
- 子进程保持存活 120 秒，确保分享窗口不被关闭

注意：
- 仅支持 macOS 系统
- AirDrop 需要用户在 Mac 上选择合适的设备（无法全自动）
- iPhone 端仍然需要手动点击「接受」
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, List, Dict

# 辅助脚本路径
HELPER_SCRIPT = Path(__file__).parent / "airdrop_share_helper.py"


def send_caption_via_airdrop(caption_text: str, caption_name: str = "TikTok文案") -> Optional[str]:
    """
    通过 AirDrop 发送文案到 iPhone（使用 NSSharingService + 独立子进程）

    流程：
    1. 创建临时 .txt 文件，写入文案内容
    2. 启动 airdrop_share_helper.py 子进程，弹出 AirDrop 分享面板
    3. 分享面板完全可交互（标准系统分享窗口）
    4. 用户在面板中选择目标设备完成发送

    Args:
        caption_text: 文案文本内容
        caption_name: 文案名称（用作文件名）

    Returns:
        Optional[str]: 临时文件路径（成功或兜底），失败返回 None
    """
    # 1. 创建临时文件（文件名包含文案名称，方便识别）
    safe_name = "".join(c for c in caption_name if c.isalnum() or c in " _-") or "文案"
    tmp_dir = tempfile.gettempdir()
    tmp_file = Path(tmp_dir) / f"{safe_name}.txt"

    # 写入文案内容
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write(caption_text)

    file_str = str(tmp_file)

    # 2. 启动独立子进程调用 NSSharingService API
    #    子进程会初始化自己的 NSApplication，与 Flask 主进程隔离
    try:
        proc = subprocess.Popen(
            [sys.executable, str(HELPER_SCRIPT), file_str],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # 等待子进程启动并完成初始化（不等待它结束）
        # 子进程会保持运行 120 秒以维持分享窗口
        import time
        time.sleep(1.0)  # 给 NSApplication 初始化留时间

        # 检查子进程是否立即崩溃
        poll = proc.poll()
        if poll is not None and poll != 0:
            stderr = proc.stderr.read() if proc.stderr else ""
            print(f"[AirDrop] 子进程异常退出 (code={poll}): {stderr.strip()}")

            # 兜底：在 Finder 中显示文件
            subprocess.run(["open", "-R", file_str])
            print("[AirDrop] 兜底：已在 Finder 中显示文件")
            return file_str if tmp_file.exists() else None

        # 子进程正常运行中
        print(f"[AirDrop] NSSharingService 已由子进程调起，文件: {tmp_file.name}")
        return file_str

    except FileNotFoundError:
        print("[AirDrop] Python 不可用")
        # 兜底：在 Finder 中显示文件
        subprocess.run(["open", "-R", file_str])
        return file_str if tmp_file.exists() else None
    except Exception as e:
        print(f"[AirDrop] 发送异常: {e}")
        # 兜底：在 Finder 中显示文件
        try:
            subprocess.run(["open", "-R", file_str])
        except Exception:
            pass
        return file_str if tmp_file.exists() else None


def send_caption_via_shortcuts(caption_text: str, caption_name: str = "TikTok文案") -> bool:
    """
    通过 macOS「快捷指令」发送文案（如果用户创建了快捷指令）
    
    备选方案：比 AppleScript 更稳定。
    需要用户先在 macOS 上创建一个快捷指令，名称为「发送TikTok文案」，
    内容为：接收文本输入 → 存储为文件 → 共享到 AirDrop。
    
    Args:
        caption_text: 文案文本内容
        caption_name: 文案名称
        
    Returns:
        bool: 是否成功调起快捷指令
    """
    try:
        result = subprocess.run(
            ["shortcuts", "run", "发送TikTok文案", "-i", caption_text],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def cleanup_temp_file(file_path: str):
    """清理临时文件"""
    try:
        p = Path(file_path)
        if p.exists():
            p.unlink()
    except Exception:
        pass
