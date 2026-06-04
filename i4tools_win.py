#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i4Tools (爱思助手) GUI 自动化 — Windows 版本
============================================
通过 pygetwindow + pyautogui 控制爱思助手界面，
替代 macOS 版本的 AppleScript 实现。

依赖: pygetwindow, pyautogui, opencv-python-headless, Pillow
"""

import time
import sys
from pathlib import Path

# Windows 专属依赖
try:
    import pygetwindow as gw
except ImportError:
    gw = None

try:
    import pyautogui
    pyautogui.FAILSAFE = True  # 鼠标移到屏幕角落时抛出异常
except ImportError:
    pyautogui = None


class I4ToolsWinController:
    """Windows 版爱思助手控制器

    用法:
        ctrl = I4ToolsWinController()
        ctrl.activate()
        ctrl.click_import_button()
        ctrl.select_file_in_dialog("C:\\videos\\test.mp4")
    """

    WINDOW_TITLE = "爱思助手"  # i4Tools 主窗口标题
    DIALOG_TITLE = "打开"       # Windows 文件选择对话框标题
    # 备选标题（不同版本可能不同）
    ALT_TITLES = ["i4Tools", "爱思助手7.0", "爱思助手8.0"]

    def __init__(self, activate_delay: float = 0.5):
        """
        参数:
            activate_delay: 窗口激活后的等待时间（秒）
        """
        if gw is None:
            raise ImportError(
                "pygetwindow 未安装。请在 Windows 上运行:\n"
                "  pip install pygetwindow pyautogui opencv-python-headless Pillow"
            )
        self.activate_delay = activate_delay

    # ---- 窗口管理 ----

    def _find_window(self):
        """查找爱思助手主窗口

        返回:
            pygetwindow.Window 或 None
        """
        for title in [self.WINDOW_TITLE] + self.ALT_TITLES:
            windows = gw.getWindowsWithTitle(title)
            if windows:
                return windows[0]
        return None

    def activate(self) -> bool:
        """激活爱思助手窗口（置顶+聚焦）

        返回:
            bool: 成功为 True
        """
        win = self._find_window()
        if win is None:
            print(f"❌ 未找到爱思助手窗口 (标题含 '{self.WINDOW_TITLE}')")
            print("   请确保爱思助手已启动并打开到设备管理页面。")
            return False

        try:
            # 如果窗口最小化，先恢复
            if win.isMinimized:
                win.restore()
            win.activate()
            time.sleep(self.activate_delay)
            return True
        except Exception as e:
            print(f"⚠️ 激活窗口异常: {e}")
            return False

    def is_window_visible(self) -> bool:
        """检查窗口是否可见"""
        win = self._find_window()
        return win is not None and not win.isMinimized

    # ---- 点击操作 ----

    def click_position(self, x: int, y: int):
        """点击屏幕绝对坐标"""
        pyautogui.click(x, y)
        time.sleep(0.3)

    def click_image(self, image_path: str, confidence: float = 0.85, timeout: float = 5.0):
        """通过模板图像匹配点击

        参数:
            image_path: 模板图片路径（PNG 截图）
            confidence: 匹配置信度 (0-1)
            timeout: 超时时间（秒）

        返回:
            bool: 找到并点击为 True
        """
        if pyautogui is None:
            print("⚠️ pyautogui 未安装，无法使用图像识别")
            return False

        start = time.time()
        while time.time() - start < timeout:
            try:
                loc = pyautogui.locateCenterOnScreen(image_path, confidence=confidence)
                if loc:
                    pyautogui.click(loc)
                    time.sleep(0.3)
                    return True
            except pyautogui.ImageNotFoundException:
                pass
            except Exception as e:
                print(f"  图像匹配异常: {e}")

            time.sleep(0.3)

        print(f"  ⏰ 超时: 未在屏幕找到 {Path(image_path).name}")
        return False

    # ---- 爱思助手特定操作 ----

    def click_import_button(self) -> bool:
        """
        点击"导入"按钮（弹出文件选择对话框）

        实现策略：
        优先用图像识别，失败则尝试用 Tab 键导航到按钮区 + 快捷键。
        """
        # 策略1: 图像识别（需要先截图保存按钮模板）
        templates_dir = Path(__file__).parent / "templates"
        import_btn = templates_dir / "i4tools_import_btn.png"
        if import_btn.exists():
            if self.click_image(str(import_btn), confidence=0.8):
                return True

        # 策略2: 键盘快捷键（某些版本支持 Ctrl+I）
        print("  🖱️ 尝试 Ctrl+I 导入...")
        pyautogui.hotkey("ctrl", "i")
        time.sleep(0.5)
        # 检查是否有对话框弹出（简单延时处理）
        return True

    def select_file_in_dialog(self, file_path: str) -> bool:
        """
        在 Windows 文件选择对话框中定位并选择文件

        步骤:
        1. 等待对话框出现
        2. Ctrl+L 聚焦地址栏
        3. 输入完整路径
        4. Enter 确认

        参数:
            file_path: 要选择的文件完整路径

        返回:
            bool: 成功为 True
        """
        # 等待对话框出现
        time.sleep(0.5)

        # 尝试激活对话框窗口
        dlg_windows = gw.getWindowsWithTitle(self.DIALOG_TITLE)
        if not dlg_windows:
            # 尝试其他可能的中文标题
            for title in ["打开", "选择文件", "导入文件", "Open"]:
                dlg_windows = gw.getWindowsWithTitle(title)
                if dlg_windows:
                    break

        if dlg_windows:
            dlg_windows[0].activate()
            time.sleep(0.3)

        # Ctrl+L → 聚焦到地址栏
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.2)

        # 输入完整文件路径
        pyautogui.write(file_path)
        time.sleep(0.2)

        # Enter 确认
        pyautogui.press("enter")
        time.sleep(0.5)

        return True

    def confirm_import(self) -> bool:
        """确认导入（点击"保存"或"确定"按钮）"""
        # 通常文件选择对话框在选择文件并回车后会自动关闭
        # 某些版本的 i4Tools 需要额外确认
        time.sleep(0.5)
        pyautogui.press("enter")
        return True

    # ---- 批量导入 ----

    def import_single_video(self, video_path: str) -> bool:
        """导入单个视频文件（完整流程）

        返回:
            bool: 成功为 True
        """
        video_name = Path(video_path).name
        print(f"📱 准备导入: {video_name}")

        # 1. 激活窗口
        if not self.activate():
            return False

        # 2. 点击导入按钮
        print("🖱️ 点击导入按钮...")
        if not self.click_import_button():
            print("⚠️ 导入按钮点击失败，尝试备选方案...")

        # 3. 文件选择对话框
        print("📂 选择文件...")
        self.select_file_in_dialog(video_path)

        # 4. 确认
        self.confirm_import()

        print(f"✅ 导入流程完成: {video_name}")
        return True

    def import_videos_batch(self, video_paths: list) -> int:
        """批量导入多个视频

        返回:
            int: 成功导入的数量
        """
        print(f"🎬 开始批量导入 {len(video_paths)} 个视频")

        success_count = 0
        for i, path in enumerate(video_paths, 1):
            print(f"\n[{i}/{len(video_paths)}] ", end="")
            if self.import_single_video(path):
                success_count += 1
            time.sleep(1)

        print(f"\n\n📊 导入完成: {success_count}/{len(video_paths)} 成功")
        return success_count


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python i4tools_win.py <视频文件路径>")
        print("      python i4tools_win.py batch <视频1> <视频2> ...")
        sys.exit(1)

    ctrl = I4ToolsWinController()

    if sys.argv[1] == "batch":
        paths = sys.argv[2:]
        ctrl.import_videos_batch(paths)
    else:
        ctrl.import_single_video(sys.argv[1])
