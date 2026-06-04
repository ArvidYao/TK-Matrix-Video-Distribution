#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频下载管理模块
================
提供视频下载目录设置持久化、下载后自动重命名为"日期+标题.mp4"格式、
以及已下载视频的列表查询功能。

命名规则：
    下载后的文件自动重命名为: YYYYMMDD_视频标题.mp4
    示例: 20260604_今天的美食探店.mp4

目录设置：
    用户可通过 API 或 Web 界面自由指定下载目录，
    设置持久化到 downloads/download_settings.json。
"""

import os
import re
import json
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("TKDistributor.Download")

# ── 路径常量 ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DOWNLOAD_DIR = os.path.join(SCRIPT_DIR, "downloads", "videos")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "downloads", "download_settings.json")

# 文件系统非法字符（Windows + macOS + Linux）
_ILLEGAL_CHARS_PATTERN = re.compile(r'[\\/:*?"<>|]')
# 多空白符压缩
_MULTI_SPACE_PATTERN = re.compile(r'\s+')


# ============================================================
# 下载设置管理
# ============================================================

class DownloadSettings:
    """下载目录持久化设置管理。

    设置存储在 downloads/download_settings.json，与主 config.yaml 独立，
    避免与视频源目录 (video_source_dir) 等其他配置混淆。
    """

    def __init__(self):
        self._settings = {}
        self._load()

    def _load(self):
        """从 JSON 文件加载设置"""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    self._settings = json.load(f)
                logger.debug(f"已加载下载设置: {self._settings.get('download_dir')}")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"下载设置文件损坏，使用默认值: {e}")
                self._settings = {}
        else:
            self._settings = {}

    def _save(self):
        """保存设置到 JSON 文件"""
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._settings, f, ensure_ascii=False, indent=2)

    def get_download_dir(self) -> str:
        """获取当前设置的下载目录。

        如果用户从未设置过，返回默认目录 (downloads/videos/)。
        如果目录不存在，自动创建。

        Returns:
            str: 下载目录的绝对路径
        """
        path = self._settings.get("download_dir")
        if not path:
            path = DEFAULT_DOWNLOAD_DIR

        # 相对路径转绝对路径
        if not os.path.isabs(path):
            path = os.path.join(SCRIPT_DIR, path)

        # 确保目录存在
        os.makedirs(path, exist_ok=True)
        return path

    def set_download_dir(self, path: str):
        """设置下载目录并持久化。

        Args:
            path: 用户选择的目录路径（可以是相对或绝对路径）
        """
        if not os.path.isabs(path):
            path = os.path.abspath(os.path.join(SCRIPT_DIR, path))

        self._settings["download_dir"] = path
        self._save()
        os.makedirs(path, exist_ok=True)
        logger.info(f"下载目录已更新: {path}")

    def reset_to_default(self):
        """重置为默认目录"""
        self._settings.pop("download_dir", None)
        self._save()
        logger.info("下载目录已重置为默认值")

    def get_settings_dict(self) -> dict:
        """获取当前设置的字典表示（用于 API 返回）。

        Returns:
            dict: {"download_dir": str, "is_default": bool, "exists": bool}
        """
        current_dir = self.get_download_dir()
        return {
            "download_dir": current_dir,
            "is_default": not self._settings.get("download_dir"),
            "exists": os.path.isdir(current_dir),
        }


# ============================================================
# 文件名清洗
# ============================================================

def sanitize_filename(title: str) -> str:
    """清理文件名，移除非法字符，适合用作文件系统路径。

    处理规则:
        1. 移除 \\ / : * ? " < > | 等非法字符 → 替换为 '-'
        2. 移除换行、制表等控制字符
        3. 压缩多个空格为单个空格
        4. 限制最大长度 80 字符（保留 .mp4 扩展名空间）
        5. 避免仅剩 '.' 或空格的文件名

    Args:
        title: 原始视频标题/描述

    Returns:
        str: 清洗后的安全文件名（不含扩展名）
    """
    if not title:
        return "untitled"

    safe = title.strip()

    # 替换非法字符
    safe = _ILLEGAL_CHARS_PATTERN.sub('-', safe)

    # 移除控制字符
    safe = safe.replace('\n', '').replace('\r', '').replace('\t', ' ')

    # 压缩多个空格
    safe = _MULTI_SPACE_PATTERN.sub(' ', safe)

    # 去掉首尾点和空格
    safe = safe.strip('. ')

    # 限制长度
    if len(safe) > 80:
        # 尽量在单词边界截断
        truncated = safe[:80]
        last_space = truncated.rfind(' ')
        if last_space > 40:
            safe = truncated[:last_space]
        else:
            safe = truncated

    safe = safe.strip('. ')

    if not safe:
        safe = "untitled"

    return safe


# ============================================================
# 视频重命名
# ============================================================

def rename_downloaded_video(
    filepath: str,
    date_str: str = None,
    title: str = None,
) -> dict:
    """将已下载的视频重命名为「日期_标题.扩展名」格式。

    Args:
        filepath: 下载后的原始文件完整路径
        date_str:  日期字符串，格式 YYYYMMDD（默认使用今天）
        title:     视频标题文本（会经 sanitize_filename 清洗）

    Returns:
        dict: {
            "filepath": str,    # 重命名后的完整路径
            "filename": str,    # 重命名后的文件名
            "original": str,    # 原始文件名
            "date_used": str,   # 使用的日期
        }

    Raises:
        FileNotFoundError: 原始文件不存在
    """
    filepath_obj = Path(filepath)

    if not filepath_obj.exists():
        raise FileNotFoundError(f"下载文件不存在，无法重命名: {filepath}")

    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    ext = filepath_obj.suffix.lower()  # e.g. ".mp4"
    safe_title = sanitize_filename(title or filepath_obj.stem)
    new_name = f"{date_str}_{safe_title}{ext}"
    new_path = filepath_obj.parent / new_name

    # 避免重名：追加序号
    counter = 1
    while new_path.exists():
        new_name = f"{date_str}_{safe_title}_{counter}{ext}"
        new_path = filepath_obj.parent / new_name
        counter += 1

    filepath_obj.rename(new_path)
    original_name = filepath_obj.name

    logger.info(f"✓ 视频已重命名: {original_name} → {new_name}")

    return {
        "filepath": str(new_path),
        "filename": new_name,
        "original": original_name,
        "date_used": date_str,
    }


# ============================================================
# 便捷方法
# ============================================================

def get_effective_download_dir(save_dir: str = None) -> str:
    """根据优先级获取实际使用的下载目录。

    优先级:
        1. save_dir 参数（用户通过 API 显式传入）
        2. 持久化设置（download_settings.json）
        3. 默认目录（downloads/videos/）

    Args:
        save_dir: 用户指定目录（可选）

    Returns:
        str: 实际下载目录绝对路径
    """
    if save_dir and save_dir.strip():
        path = save_dir
        if not os.path.isabs(path):
            path = os.path.abspath(os.path.join(SCRIPT_DIR, path))
        os.makedirs(path, exist_ok=True)
        return path

    settings = DownloadSettings()
    return settings.get_download_dir()


# ============================================================
# 自检 (python3 download_manager.py)
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("视频下载管理模块 - 自检")
    print("=" * 60)

    # 1. 默认目录
    print(f"\n[1] 默认下载目录: {DEFAULT_DOWNLOAD_DIR}")
    print(f"    存在: {'✓' if os.path.isdir(DEFAULT_DOWNLOAD_DIR) else '✗ (将在首次使用时创建)'}")

    # 2. 设置存取
    settings = DownloadSettings()
    print(f"\n[2] 当前设置: {settings.get_settings_dict()}")

    # 3. 文件名清洗
    test_titles = [
        "今天的美食探店/超棒！",
        "My Video: Best Of 2024???",
        "\t\n带控制字符\n的标题\r",
        "a" * 120 + "超长标题",
        "",
        "   spaces   only   ",
    ]
    print(f"\n[3] 文件名清洗测试:")
    for t in test_titles:
        clean = sanitize_filename(t)
        print(f"    '{t[:50]}...'  →  '{clean}'")

    # 4. 测试重命名逻辑（不会真的执行）
    print(f"\n[4] 重命名格式示例:")
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    print(f"    日期: {today}")
    print(f"    格式: {today}_标题.mp4")
    print(f"    示例: {today}_今天的美食探店.mp4")

    print("\n" + "=" * 60)
    print("自检完成")
    print("=" * 60)
