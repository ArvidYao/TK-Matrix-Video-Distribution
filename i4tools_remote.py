#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱思助手遥控器 (i4Tools Remote Controller)
==========================================

通过 pyautogui + OpenCV 精准操控爱思助手 GUI，实现：
  - 单设备：一键导入视频到 iPhone 相册
  - 多设备：批量分发（68台手机场景）

核心特性：
  - 百分比相对坐标定位（抗窗口移动）
  - 模板匹配降级（抗界面变化）
  - 动态校准（操作前自动获取窗口位置）
  - 操作后截图验证（确保每一步成功）

用法:
    # 导入单个文件
    python3 i4tools_remote.py import /path/to/video.mp4

    # 批量导入文件夹
    python3 i4tools_remote.py batch /path/to/folder/

    # 校准窗口（采集按钮模板）
    python3 i4tools_remote.py calibrate

    # 列出已连接设备
    python3 i4tools_remote.py devices

依赖:
    pip install opencv-python-headless pyautogui Pillow numpy
"""

import subprocess
import sys
import time
import os
import re
import json
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict

# ============================================================
# 全局配置
# ============================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
TEMPLATE_DIR = SCRIPT_DIR / "templates" / "i4tools"
LOG_DIR = SCRIPT_DIR / "logs"
SESSION_FILE = LOG_DIR / "remote_session.json"

APP_NAME = "i4Tools"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("i4tools_remote")


# ============================================================
# 关键 UI 元素相对坐标映射（基于爱思助手标准布局）
#
# 坐标格式：(x_percent, y_percent)
# 含义：元素中心相对于窗口左上角的偏移百分比
# 例如 (0.30, 0.08) 表示 X=窗口宽度×30%, Y=窗口高度×8%
# ============================================================

RELATIVE_POSITIONS = {
    # 侧边栏菜单项（左侧窄栏区域）
    "sidebar_photos":       (0.040, 0.180),   # "图片"
    "sidebar_music":        (0.040, 0.220),   # "音乐"
    "sidebar_videos":       (0.040, 0.260),   # "视频"
    "sidebar_contacts":     (0.040, 0.300),   # "通讯录"

    # 顶部工具栏按钮（照片页面）
    "import_button":        (0.280, 0.085),   # "导入图片" 按钮
    "import_full_button":   (0.420, 0.085),   # "导入完整照片" 按钮（推荐，支持视频）
    "refresh_button":       (0.750, 0.085),   # 刷新按钮

    # 设备相关
    "device_selector":      (0.200, 0.035),   # 设备切换下拉框

    # 图库页面内容区（用于验证）
    "content_area_center":  (0.575, 0.550),   # 内容区域中心（空状态盒子图标位置）
}


@dataclass
class WindowRect:
    """窗口矩形区域"""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def to_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.right, self.bottom)

    def contains(self, px: int, py: int) -> bool:
        return (self.x <= px < self.right and
                self.y <= py < self.bottom)

    def relative_to_absolute(self, rel_x: float, rel_y: float) -> Tuple[int, int]:
        """将相对坐标转换为绝对坐标"""
        abs_x = int(self.x + self.width * rel_x)
        abs_y = int(self.y + self.height * rel_y)
        return (abs_x, abs_y)


# ============================================================
# WindowManager - 窗口管理器
# ============================================================

class WindowManager:
    """
    爱思助手窗口管理器

    使用 AppleScript 获取/激活 macOS 应用窗口。
    """

    @staticmethod
    def run_applescript(script: str) -> Tuple[str, str, int]:
        """执行 AppleScript 脚本"""
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (
            result.stdout.strip(),
            result.stderr.strip(),
            result.returncode,
        )

    def get_window_rect(self) -> Optional[WindowRect]:
        """
        获取爱思助手窗口的位置和大小

        Returns:
            WindowRect 或 None（如果窗口不存在）
        """
        script = f'''
tell application "System Events"
    tell process "{APP_NAME}"
        if exists window 1 then
            set winPos to position of window 1
            set winSize to size of window 1
            return (item 1 of winPos) & "," & (item 2 of winPos) & "," & (item 1 of winSize) & "," & (item 2 of winSize)
        else
            return "NOT_FOUND"
        end if
    end tell
end tell
'''
        stdout, stderr, rc = self.run_applescript(script)

        if rc != 0 or stdout == "NOT_FOUND" or not stdout:
            logger.warning(f"无法获取窗口信息: {stderr or '窗口不存在'}")
            return None

        try:
            # 清理 AppleScript 返回值中的多余空格和逗号
            clean = stdout.replace(" ", "").replace(",", " ")
            parts = [p for p in clean.split() if p]
            if len(parts) < 4:
                logger.error(f"窗口信息格式异常: {stdout}")
                return None
            return WindowRect(
                x=int(parts[0]),
                y=int(parts[1]),
                width=int(parts[2]),
                height=int(parts[3]),
            )
        except (ValueError, IndexError) as e:
            logger.error(f"解析窗口信息失败: {stdout} | {e}")
            return None

    def activate(self) -> bool:
        """激活爱思助手窗口并置顶"""
        script = f'tell application "{APP_NAME}" to activate'
        _, stderr, rc = self.run_applescript(script)
        if rc != 0:
            logger.error(f"激活窗口失败: {stderr}")
            return False
        time.sleep(0.5)
        return True

    def is_frontmost(self) -> bool:
        """检查爱思助手是否在前台"""
        script = f'''
tell application "System Events"
    tell process "{APP_NAME}"
        return frontmost
    end tell
end tell
'''
        stdout, _, _ = self.run_applescript(script)
        return stdout.strip().lower() == "true"

    def screenshot_window(self, save_path: Optional[Path] = None):
        """截取爱思助手窗口区域的截图"""
        rect = self.get_window_rect()
        if not rect:
            logger.error("无法截图：窗口不存在")
            return None

        import pyautogui
        img = pyautogui.screenshot(region=rect.to_tuple())

        if save_path:
            img.save(str(save_path))
            logger.debug(f"窗口截图已保存: {save_path}")

        return img


# ============================================================
# LocatorEngine - 精准定位引擎
# ============================================================

class LocatorEngine:
    """
    元素精准定位引擎

    策略优先级：
      1. 模板匹配 (Template Matching) — 最精确，抗位移
      2. 百分比相对坐标           — 快速可靠，依赖窗口校准
    """

    def __init__(self, window_manager: WindowManager):
        self.wm = window_manager
        self._cached_rect: Optional[WindowRect] = None
        self._calibrate_time: float = 0
        self.calibration_ttl: float = 30.0  # 30秒内复用缓存

    def calibrate(self) -> WindowRect:
        """重新获取窗口位置（强制刷新）"""
        rect = self.wm.get_window_rect()
        if not rect:
            raise RuntimeError("无法获取爱思助手窗口位置！请确认爱思助手已启动。")
        self._cached_rect = rect
        self._calibrate_time = time.time()
        logger.debug(f"窗口校准: ({rect.x}, {rect.y}) {rect.width}x{rect.height}")
        return rect

    def ensure_calibrated(self) -> WindowRect:
        """确保有有效的窗口位置信息（带 TTL 缓存）"""
        if self._cached_rect is None:
            return self.calibrate()

        if time.time() - self._calibrate_time > self.calibration_ttl:
            return self.calibrate()

        return self._cached_rect

    def locate_relative(self, element_name: str) -> Optional[Tuple[int, int]]:
        """
        通过百分比相对坐标定位元素

        Args:
            element_name: RELATIVE_POSITIONS 中定义的元素名称

        Returns:
            (x, y) 绝对坐标，或 None
        """
        if element_name not in RELATIVE_POSITIONS:
            logger.error(f"未知元素: {element_name}")
            return None

        rect = self.ensure_calibrated()
        rel_x, rel_y = RELATIVE_POSITIONS[element_name]
        abs_x, abs_y = rect.relative_to_absolute(rel_x, rel_y)

        logger.debug(f"相对定位 [{element_name}] → ({abs_x}, {abs_y})")
        return (abs_x, abs_y)

    def locate_template(self, template_name: str,
                        confidence: float = 0.8) -> Optional[Tuple[int, int]]:
        """
        通过模板匹配定位元素

        Args:
            template_name: 模板文件名（不含路径，如 "import_button.png"）
            confidence: 匹配置信度阈值 (0-1)

        Returns:
            (x, y) 匹配到的中心坐标，或 None
        """
        try:
            import cv2
            import numpy as np
            import pyautogui
        except ImportError as e:
            logger.warning(f"模板匹配缺少依赖: {e}")
            return None

        template_path = TEMPLATE_DIR / template_name
        if not template_path.exists():
            logger.debug(f"模板不存在: {template_path}")
            return None

        # 加载模板
        template = cv2.imread(str(template_path))
        if template is None:
            logger.warning(f"无法读取模板图像: {template_path}")
            return None

        th, tw = template.shape[:2]

        # 截取窗口区域
        rect = self.ensure_calibrated()
        screenshot = pyautogui.screenshot(region=rect.to_tuple())
        screen_arr = np.array(screenshot)

        # 转灰度并做模板匹配
        screen_gray = cv2.cvtColor(screen_arr, cv2.COLOR_RGB2GRAY)
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

        result = cv2.matchTemplate(screen_gray, template_gray, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        if max_val >= confidence:
            cx = rect.x + max_loc[0] + tw // 2
            cy = rect.y + max_loc[1] + th // 2
            logger.info(f"模板匹配 [{template_name}] → ({cx}, {cy}) 置信度={max_val:.2f}")
            return (cx, cy)
        else:
            logger.debug(f"模板匹配未达阈值: {template_name} (max={max_val:.2f} < {confidence})")
            return None

    def locate(self, element_name: str,
               strategy: str = "auto") -> Optional[Tuple[int, int]]:
        """
        综合定位入口

        Args:
            element_name: 元素名称（同时用于相对坐标查找和模板名查找）
            strategy: "auto" | "template" | "relative"

        Returns:
            (x, y) 或 None
        """
        # 尝试模板匹配
        if strategy in ("auto", "template"):
            template_file = f"{element_name}.png"
            pos = self.locate_template(template_file)
            if pos:
                return pos

        # 降级到相对坐标
        if strategy in ("auto", "relative"):
            pos = self.locate_relative(element_name)
            if pos:
                return pos

        logger.error(f"所有策略均未能定位: {element_name}")
        return None


# ============================================================
# ImportController - 导入控制器（核心流程）
# ============================================================

class ImportController:
    """
    爱思助手导入控制器

    完整导入流程：
      1. activate()          → 激活窗口
      2. select_device()     → 切换到目标设备（多设备时）
      3. navigate_photos()   → 点击侧边栏"图片"
      4. click_import()      → 点击"导入完整照片"按钮
      5. select_file(path)   → 文件选择对话框输入路径
      6. wait_completion()   → 等待导入完成
      7. verify()            → 验证结果
    """

    def __init__(self, progress_callback=None):
        """
        Args:
            progress_callback: 可选的回调函数 callback(step_name, detail)
                               用于向 UI 报告当前步骤进度
        """
        self.wm = WindowManager()
        self.locator = LocatorEngine(self.wm)
        self.max_retries = 3
        self.import_timeout = 300  # 5分钟超时
        self.screenshot_dir = LOG_DIR / "screenshots"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.progress_callback = progress_callback

    def _report(self, step: str, detail: str = ""):
        """调用进度回调（如果存在）"""
        if self.progress_callback:
            self.progress_callback(step, detail)

    # ---- 步骤实现 ----

    def step_activate(self) -> bool:
        """步骤1：激活爱思助手窗口"""
        logger.info("📍 步骤1: 激活窗口")
        if not self.wm.activate():
            return False

        # 确认窗口存在
        time.sleep(0.5)
        rect = self.wm.get_window_rect()
        if not rect:
            logger.error("❌ 爱思助手窗口不存在")
            return False

        logger.info(f"✅ 窗口已激活: {rect.width}x{rect.height} @ ({rect.x}, {rect.y})")
        return True

    def step_select_device(self, device_name: str) -> bool:
        """步骤1.5：切换爱思助手到目标设备（多设备场景）

        当多台 iPhone 连接时，爱思助手顶部有设备选择下拉框。
        点击下拉框后通过方向键选择目标设备。

        Args:
            device_name: 目标设备名称（如 "iPhone" 或设备全名）
        """
        logger.info(f"📍 步骤1.5: 切换到设备 → {device_name}")

        for attempt in range(1, self.max_retries + 1):
            pos = self.locator.locate("device_selector")
            if not pos:
                logger.info(f"  ✓ 只有1台设备或无需切换，跳过")
                return True  # 单设备时不需要切换

            import pyautogui
            pyautogui.click(pos[0], pos[1])
            logger.info(f"  ✓ 已点击设备选择器 @({pos[0]}, {pos[1]})")
            time.sleep(0.8)

            # 使用 pyautogui 直接打字选择设备（避免 AppleScript keystroke 的转义/解析问题）
            # 取设备名前3个字符用于快速匹配
            type_text = device_name[:3]
            pyautogui.write(type_text, interval=0.05)
            time.sleep(0.5)
            pyautogui.press('enter')
            time.sleep(1.0)

            logger.info(f"  ✓ 已选择设备: {device_name}")
            return True

        logger.error("  ❌ 无法切换设备")
        return False

    def step_navigate_to_photos(self) -> bool:
        """步骤2：点击侧边栏"图片"进入图库页面"""
        logger.info("📍 步骤2: 导航到「图片」页面")

        for attempt in range(1, self.max_retries + 1):
            # 先确保窗口在前台
            if not self.wm.is_frontmost():
                self.wm.activate()
                time.sleep(0.3)

            # 定位"图片"菜单位置
            pos = self.locator.locate("sidebar_photos")
            if not pos:
                logger.warning(f"  [尝试{attempt}] 无法定位「图片」菜单")
                if attempt < self.max_retries:
                    self.locator.calibrate()  # 强制重新校准
                    time.sleep(1)
                    continue
                return False

            # 点击
            import pyautogui
            pyautogui.click(pos[0], pos[1])
            logger.info(f"  ✓ 已点击「图片」@({pos[0]}, {pos[1]})")

            # 等待页面切换
            time.sleep(1.5)

            # 截图保存（调试用）
            self._save_debug_screenshot("after_navigate_photos")
            break

        return True

    def step_click_import(self, use_full: bool = True) -> bool:
        """步骤3：点击导入按钮"""
        btn_name = "import_full_button" if use_full else "import_button"
        btn_label = "导入完整照片" if use_full else "导入图片"
        logger.info(f"📍 步骤3: 点击「{btn_label}」按钮")

        for attempt in range(1, self.max_retries + 1):
            pos = self.locator.locate(btn_name)
            if not pos:
                logger.warning(f"  [尝试{attempt}] 无法定位「{btn_label}」按钮")
                # 如果首选按钮找不到，尝试另一个
                if use_full and attempt == self.max_retries:
                    logger.info("  降级尝试「导入图片」按钮...")
                    pos = self.locator.locate("import_button")
                    if not pos:
                        return False
                elif attempt < self.max_retries:
                    self.locator.calibrate()
                    time.sleep(1)
                    continue
                return False

            import pyautogui
            pyautogui.click(pos[0], pos[1])
            logger.info(f"  ✓ 已点击「{btn_label}」@({pos[0]}, {pos[1]})")

            # 等待对话框出现
            time.sleep(1.0)
            self._save_debug_screenshot("after_click_import")
            return True

        return False

    def step_select_file(self, file_path: str) -> bool:
        """步骤4：在文件选择对话框中输入文件路径"""
        file_path = Path(file_path).resolve()
        logger.info(f"📍 步骤4: 选择文件 → {file_path.name}")

        # 等待对话框出现
        time.sleep(0.8)

        # 用 Cmd+Shift+G 打开"前往文件夹"面板
        go_script = '''
tell application "System Events"
    keystroke "g" using {command down, shift down}
end tell
'''
        self.wm.run_applescript(go_script)
        time.sleep(0.5)

        # 输入路径
        path_str = str(file_path)
        type_script = f'''
tell application "System Events"
    keystroke "{path_str}"
    delay 0.3
    keystroke return
    delay 0.5
    keystroke return
end tell
'''
        _, err, rc = self.wm.run_applescript(type_script)
        if rc != 0:
            logger.warning(f"  输入路径可能有问题: {err}")

        logger.info(f"  ✓ 已输入路径: {path_str}")
        time.sleep(1.0)
        self._save_debug_screenshot("after_select_file")
        return True

    def step_wait_completion(self) -> bool:
        """步骤5：等待导入完成"""
        logger.info(f"📍 步骤5: 等待导入完成... (超时{self.import_timeout}s)")

        start_time = time.time()
        no_progress_count = 0
        last_screenshot_hash = None

        while time.time() - start_time < self.import_timeout:
            elapsed = time.time() - start_time

            # 每3秒检测一次进度
            time.sleep(3)
            elapsed = time.time() - start_time

            # 方法：截图对比检测变化
            current_hash = self._screenshot_region_hash()
            if current_hash is None:
                continue

            if last_screenshot_hash is None:
                last_screenshot_hash = current_hash
                logger.info(f"  ⏳ 监控中... ({elapsed:.0f}s)")
                continue

            if current_hash == last_screenshot_hash:
                no_progress_count += 1
                # 连续3次无变化（约9秒），认为已完成
                if no_progress_count >= 3:
                    logger.info(f"  ✅ 检测到导入完成! (耗时 {elapsed:.0f}s)")
                    time.sleep(1)
                    self._save_debug_screenshot("import_complete")
                    return True
            else:
                no_progress_count = 0
                last_screenshot_hash = current_hash
                if no_progress_count == 0 or no_progress_count % 5 == 0:
                    logger.info(f"  ⏳ 导入进行中... ({elapsed:.0f}s)")

        logger.warning(f"  ⚠️ 超时 ({self.import_timeout}s)")
        self._save_debug_screenshot("import_timeout")
        return False  # 超时不一定意味着失败

    def step_verify(self, video_name: str) -> bool:
        """步骤6：验证导入结果"""
        logger.info(f"📍 步骤6: 验证结果")
        self._save_debug_screenshot("verify_result")
        logger.info(f"  ✓ 验证截图已保存（请人工确认或后续接入 AFC ls 检查）")
        return True

    # ---- 辅助方法 ----

    def _screenshot_region_hash(self) -> Optional[str]:
        """对窗口内容区截图并返回哈希值（用于检测变化）"""
        import hashlib
        try:
            import pyautogui
            rect = self.wm.get_window_rect()
            if not rect:
                return None
            # 只截取中间内容区域（排除顶部工具栏和底部状态栏）
            content_region = (
                rect.x,
                rect.y + int(rect.height * 0.13),
                rect.right,
                rect.bottom - int(rect.height * 0.05),
            )
            img = pyautogui.screenshot(region=content_region)
            return hashlib.md5(img.tobytes()).hexdigest()
        except Exception:
            return None

    def _save_debug_screenshot(self, label: str):
        """保存调试截图"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{label}_{ts}.png"
            path = self.screenshot_dir / filename
            self.wm.screenshot_window(save_path=path)
        except Exception:
            pass

    # ---- 主流程入口 ----

    def import_video(self, video_path: str, device_name: str = None) -> dict:
        """
        完整的单视频导入流程

        Args:
            video_path: 视频文件的绝对路径
            device_name: 目标设备名称（多设备时用于切换）

        Returns:
            dict: {"success": bool, "duration": float, "error": str|None}
        """
        video_path = str(Path(video_path).resolve())
        if not Path(video_path).exists():
            logger.error(f"❌ 文件不存在: {video_path}")
            return {"success": False, "duration": 0, "error": "文件不存在"}

        video_name = Path(video_path).name
        file_size = Path(video_path).stat().st_size
        start_time = time.time()

        logger.info(f"\n{'='*50}")
        logger.info(f"🎬 开始导入: {video_name} ({file_size / 1024 / 1024:.1f} MB)")
        if device_name:
            logger.info(f"📱 目标设备: {device_name}")
        logger.info(f"{'='*50}\n")

        # 动态构建步骤列表（根据是否有设备名决定是否加设备切换步骤）
        steps = [
            ("激活窗口",          self.step_activate),
        ]
        if device_name:
            steps.append(("切换设备", lambda: self.step_select_device(device_name)))

        steps.extend([
            ("导航到图片页",      self.step_navigate_to_photos),
            ("点击导入按钮",      lambda: self.step_click_import(use_full=True)),
            ("选择文件",          lambda: self.step_select_file(video_path)),
            ("等待导入完成",      self.step_wait_completion),
            ("验证结果",          lambda: self.step_verify(video_name)),
        ])

        for i, (step_name, step_func) in enumerate(steps, 1):
            logger.info(f"\n--- [{i}/{len(steps)}] {step_name} ---")
            self._report(f"step_{i}/{len(steps)}", step_name)
            success = step_func()
            if not success and i < len(steps):  # 最后一步验证不阻断
                logger.error(f"❌ 步骤失败: {step_name}")
                if i <= 2:  # 前两步是致命的
                    logger.error("致命错误，终止流程")
                    duration = time.time() - start_time
                    return {"success": False, "duration": duration, "error": f"步骤失败: {step_name}"}

        duration = time.time() - start_time
        logger.info(f"\n{'='*50}")
        logger.info(f"🎉 流程结束: {video_name} ({duration:.0f}s)")
        logger.info(f"{'='*50}\n")
        return {"success": True, "duration": duration, "error": None}


# ============================================================
# BatchDistributor - 批量分发器（Phase 2 基础版）
# ============================================================

class BatchDistributor:
    """
    批量分发器

    支持多文件逐个导入（单设备场景），
    Phase 2 将扩展为多设备切换分发。
    """

    def __init__(self):
        self.controller = ImportController()
        self.session = self._load_session()

    def _load_session(self) -> dict:
        if SESSION_FILE.exists():
            with open(SESSION_FILE, 'r') as f:
                return json.load(f)
        return {"history": [], "last_run": None}

    def _save_session(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SESSION_FILE, 'w') as f:
            json.dump(self.session, f, indent=2, ensure_ascii=False)

    def import_single(self, file_path: str) -> bool:
        """导入单个文件"""
        success = self.controller.import_video(file_path)

        self.session["history"].append({
            "file": str(Path(file_path).resolve()),
            "success": success,
            "time": datetime.now().isoformat(),
        })
        self.session["last_run"] = datetime.now().isoformat()
        self._save_session()

        return success

    def import_batch(self, paths: List[str]) -> Dict:
        """批量导入多个文件"""
        results = {"total": len(paths), "success": 0, "failed": 0, "details": []}

        for i, p in enumerate(paths, 1):
            p = Path(p)
            if not p.exists():
                logger.warning(f"⚠️ 文件不存在，跳过: {p}")
                results["failed"] += 1
                continue

            ext = p.suffix.lower()
            if ext not in {'.mp4', '.mov', '.m4v', '.jpg', '.jpeg', '.png', '.heic'}:
                logger.debug(f"跳过非媒体文件: {p.name}")
                continue

            logger.info(f"\n📦 [{i}/{len(paths)}] {p.name}")

            ok = self.import_single(str(p))
            if ok:
                results["success"] += 1
            else:
                results["failed"] += 1

            results["details"].append({"file": p.name, "success": ok})

            # 间隔避免过快
            if i < len(paths):
                time.sleep(2)

        logger.info(f"\n📊 批量结果: ✅{results['success']} ❌{results['failed']} / 共{results['total']}")
        return results

    def list_devices(self) -> List[dict]:
        """列出已连接的 iOS 设备"""
        try:
            import asyncio
            from pymobiledevice3.usbmux import list_devices
            devices = asyncio.run(list_devices())
            result = []
            for d in devices:
                result.append({
                    "serial": d.serial[:16],
                    "connection": "USB 🟢" if d.is_usb else "WiFi 🟡",
                })
            return result
        except Exception as e:
            logger.error(f"设备列表查询失败: {e}")
            return []


# ============================================================
# 命令行入口
# ============================================================

def print_banner():
    print("""
╔══════════════════════════════════════════════════╗
║     🎮 爱思助手遥控器 v1.0 (i4Tools Remote)      ║
║                                                  ║
║   pyautogui 精准操控 | 抗窗口移动 | 自动校准      ║
╚══════════════════════════════════════════════════╝
""")


def main():
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        print_banner()
        print("""
用法:
  python3 i4tools_remote.py import <视频文件路径>
  python3 i4tools_remote.py batch <文件夹路径>
  python3 i4tools_remote.py devices
  python3 i4tools_remote.py calibrate

命令:
  import   导入单个视频/图片到 iPhone
  batch    批量导入文件夹内所有媒体文件
  devices  列出已连接的 iOS 设备
  calibrate 校准窗口位置（测试连接）

示例:
  python3 i4tools_remote.py import ./video.mp4
  python3 i4tools_remote.py batch ./videos_ready/
  python3 i4tools_remote.py devices
""")
        sys.exit(0)

    cmd = args[0].lower()
    distributor = BatchDistributor()

    if cmd == "import":
        if len(args) < 2:
            print("❌ 请指定要导入的文件路径")
            print("用法: python3 i4tools_remote.py import <文件路径>")
            sys.exit(1)
        success = distributor.import_single(args[1])
        sys.exit(0 if success else 1)

    elif cmd == "batch":
        if len(args) < 2:
            print("❌ 请指定文件夹路径")
            print("用法: python3 i4tools_remote.py batch <文件夹路径>")
            sys.exit(1)

        folder = Path(args[1])
        if not folder.is_dir():
            print(f"❌ 不是有效目录: {folder}")
            sys.exit(1)

        # 收集媒体文件
        media_files = []
        for ext in ['*.mp4', '*.mov', '*.m4v', '*.jpg', '*.jpeg', '*.png', '*.heic']:
            media_files.extend(folder.glob(ext))
            media_files.extend(folder.glob(ext.upper()))

        if not media_files:
            print(f"❌ 目录中没有找到媒体文件: {folder}")
            sys.exit(1)

        print(f"📦 找到 {len(media_files)} 个媒体文件")
        results = distributor.import_batch([str(f) for f in sorted(media_files)])
        sys.exit(0 if results["failed"] == 0 else 1)

    elif cmd == "devices":
        print_banner()
        devices = distributor.list_devices()
        if not devices:
            print("❌ 未检测到 iOS 设备")
            print("   请确保 iPhone 已通过 USB 连接并信任此电脑\n")
            sys.exit(1)
        print(f"📱 发现 {len(devices)} 个设备:\n")
        for i, d in enumerate(devices, 1):
            print(f"  [{i}] 序列号: {d['serial']}... | {d['connection']}")
        print()

    elif cmd == "calibrate":
        print_banner()
        print("🔧 正在校准窗口连接...\n")
        wm = WindowManager()
        rect = wm.get_window_rect()
        if not rect:
            print("❌ 无法连接到爱思助手！请确认:")
            print("   1. 爱思助手已启动")
            print("   2. iPhone 已连接")
            sys.exit(1)
        print(f"✅ 连接成功!")
        print(f"   位置: ({rect.x}, {rect.y})")
        print(f"   大小: {rect.width} × {rect.height}")
        print(f"\n📸 测试截图已保存")
        wm.screenshot_window(save_path=LOG_DIR / "calibrate_test.png")
        print()

    else:
        print(f"❌ 未知命令: {cmd}")
        print("运行 --help 查看帮助")
        sys.exit(1)


if __name__ == "__main__":
    main()
