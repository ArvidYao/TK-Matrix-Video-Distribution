#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i4Tools (爱思助手) GUI 自动化脚本
通过 AppleScript System Events 控制爱思助手界面
"""

import subprocess
import time
import sys
from pathlib import Path


def run_applescript(script: str) -> tuple:
    """执行 AppleScript，返回 (stdout, stderr, returncode)"""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def activate_i4tools():
    """激活 i4Tools 窗口"""
    stdout, stderr, rc = run_applescript('tell application "i4Tools" to activate')
    if rc != 0:
        print(f"❌ 无法激活 i4Tools: {stderr}")
        return False
    time.sleep(0.5)
    return True


def click_button_by_position(x: int, y: int):
    """点击指定位置的按钮"""
    script = f'''
tell application "System Events"
    tell process "i4Tools"
        click at {{{x}, {y}}}
    end tell
end tell
'''
    return run_applescript(script)


def click_button_by_index(index: int, in_group: bool = False):
    """点击指定索引的按钮"""
    if in_group:
        script = f'''
tell application "System Events"
    tell process "i4Tools"
        tell window 1
            tell group 1
                tell group 1
                    tell group 1
                        click button {index}
                    end tell
                end tell
            end tell
        end tell
    end tell
end tell
'''
    else:
        script = f'''
tell application "System Events"
    tell process "i4Tools"
        tell window 1
            click button {index}
        end tell
    end tell
end tell
'''
    return run_applescript(script)


def check_checkbox_by_index(index: int, checked: bool = True):
    """勾选/取消指定索引的 checkbox"""
    val = 1 if checked else 0
    script = f'''
tell application "System Events"
    tell process "i4Tools"
        tell window 1
            tell group 1
                tell group 1
                    tell group 1
                        set value of checkbox {index} to {val}
                    end tell
                end tell
            end tell
        end tell
    end tell
end tell
'''
    return run_applescript(script)


def get_checkbox_values():
    """获取所有 checkbox 的值（用于调试）"""
    script = '''
tell application "System Events"
    tell process "i4Tools"
        tell window 1
            set output to ""
            repeat with i from 1 to count of checkboxes
                try
                    set cb to checkbox i
                    set output to output & "Checkbox " & i & ": " & (value of cb) & linefeed
                end try
            end repeat
            return output
        end tell
    end tell
end tell
'''
    return run_applescript(script)


def open_file_dialog_and_select(file_path: str):
    """
    打开文件选择对话框并选择指定文件
    这是关键：爱思助手的"导入"按钮会弹出系统文件选择对话框
    """
    script = f'''
tell application "System Events"
    tell process "i4Tools"
        -- 等待文件选择对话框出现
        delay 0.5
        
        -- 获取当前激活的对话框
        set frontmost to true
        
        -- 在文件选择对话框中输入路径
        keystroke "g" using {{command down, shift down}}  -- Cmd+Shift+G (Go to folder)
        delay 0.3
        keystroke "{file_path}"
        delay 0.2
        keystroke return
        delay 0.3
        keystroke return  -- 确认选择
    end tell
end tell
'''
    return run_applescript(script)


def import_single_video(video_path: str):
    """
    通过爱思助手导入单个视频
    
    流程：
    1. 激活 i4Tools
    2. 点击"导入"按钮（弹出文件选择对话框）
    3. 在对话框中选择文件
    4. 确认导入
    """
    print(f"📱 准备导入: {Path(video_path).name}")
    
    # 1. 激活窗口
    if not activate_i4tools():
        return False
    
    # 2. 点击导入按钮（根据 UI 分析，button 4-7 在设备面板区域）
    # 先尝试点击 button 4（可能是"导入"或"添加文件"）
    print("🖱️ 点击导入按钮...")
    stdout, stderr, rc = click_button_by_index(4, in_group=True)
    if rc != 0:
        print(f"⚠️ 点击 button 4 失败: {stderr}")
    
    time.sleep(0.5)
    
    # 3. 处理文件选择对话框
    print("📂 选择文件...")
    stdout, stderr, rc = open_file_dialog_and_select(video_path)
    if rc != 0:
        print(f"⚠️ 文件选择可能有问题: {stderr}")
    
    time.sleep(1)
    
    print(f"✅ 导入流程完成: {Path(video_path).name}")
    return True


def import_videos_batch(video_paths: list):
    """批量导入多个视频"""
    print(f"🎬 开始批量导入 {len(video_paths)} 个视频")
    
    success_count = 0
    for i, path in enumerate(video_paths, 1):
        print(f"\n[{i}/{len(video_paths)}] ", end="")
        if import_single_video(path):
            success_count += 1
        time.sleep(1)  # 间隔避免过快
    
    print(f"\n\n📊 导入完成: {success_count}/{len(video_paths)} 成功")
    return success_count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 i4tools_automation.py <视频文件路径>")
        print("      python3 i4tools_automation.py batch <视频1> <视频2> ...")
        sys.exit(1)
    
    if sys.argv[1] == "batch":
        paths = sys.argv[2:]
        import_videos_batch(paths)
    else:
        import_single_video(sys.argv[1])
