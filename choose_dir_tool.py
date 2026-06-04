#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立的文件夹选择器工具（macOS 原生版）
由 Web 后端通过子进程调用，弹出原生文件夹选择对话框，
将结果写入临时文件供主进程读取。
用法: python3 choose_dir_tool.py <output_file>

无需 tkinter —— 使用系统内置 AppleScript/NSApp 弹出对话。
"""

import sys
import json
import subprocess
from pathlib import Path


def choose_directory():
    """
    弹出原生 macOS 文件夹选择器。
    优先使用 AppleScript（最快、最可靠，不依赖额外的 Python 包），
    失败时回退到 PyObjC NSOpenPanel。
    """
    script = '''
    try
        set selectedFolder to choose folder with prompt "请选择视频存放目录"
        return POSIX path of selectedFolder
    on error errMsg number errNum
        return "CANCEL:" & errNum & ":" & errMsg
    end try
    '''

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300
        )
        output = proc.stdout.strip()
        err_output = proc.stderr.strip()

        if err_output:
            return None, f"osascript 错误: {err_output[:200]}"

        if not output or output.startswith("CANCEL"):
            return None, "用户取消选择"

        selected = output.rstrip("/")
        if Path(selected).is_dir():
            return selected, None
        return None, f"路径不存在: {selected}"

    except FileNotFoundError:
        pass  # 回退到 PyObjC
    except subprocess.TimeoutExpired:
        return None, "选择超时（5分钟）"
    except Exception as e:
        return None, str(e)

    # ── 回退：PyObjC NSOpenPanel ──
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(2)  # NSApplicationActivationPolicyAccessory
        app.activateIgnoringOtherApps_(True)

        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setTitle_("选择视频存放目录")

        # 需要事件循环才能阻塞等待
        ok = panel.runModal()
        if ok == AppKit.NSModalResponseOK:
            url = panel.URLs()[0]
            return url.path().rstrip("/"), None
        return None, "用户取消选择"
    except Exception as e:
        return None, f"PyObjC 回退失败: {e}"


def main():
    output_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tk_chooser_result.json"

    selected, error = choose_directory()

    result = {
        "success": bool(selected),
        "path": selected,
    }
    if error:
        result["error"] = error

    Path(output_file).write_text(json.dumps(result, ensure_ascii=False))
    print(f"RESULT_WRITTEN:{output_file}", flush=True)


if __name__ == "__main__":
    main()
