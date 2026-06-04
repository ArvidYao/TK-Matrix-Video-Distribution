# -*- coding: utf-8 -*-
"""
TK 视频分发工具 - 核心模块（可被 CLI 和 Web 导入）
"""

import os
import sys
import json
import time
import asyncio
import logging
import shutil
import subprocess
import yaml
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

# 工具路径缓存（解决 .app 启动时 PATH 不包含 Homebrew 路径的问题）
_TOOL_CACHE: Dict[str, str] = {}
_KNOWN_PATHS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
]

def _find_tool(name: str) -> str:
    """查找工具的完整路径，找不到则回退到命令名本身"""
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    path = shutil.which(name)
    if not path:
        for pfx in _KNOWN_PATHS:
            candidate = os.path.join(pfx, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                path = candidate
                break
    if not path:
        path = name  # 回退
    _TOOL_CACHE[name] = path
    return path

# ============================================================
# 全局配置
# ============================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
LOG_DIR = SCRIPT_DIR / "logs"
HISTORY_FILE = LOG_DIR / "distribution_history.json"


def load_config() -> dict:
    """加载配置文件"""
    if not CONFIG_FILE.exists():
        return {}

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 将相对路径转为绝对路径（基于脚本所在目录）
    video_dir = Path(config.get("video_source_dir", "./videos_ready"))
    if not video_dir.is_absolute():
        config["video_source_dir_abs"] = (SCRIPT_DIR / video_dir).resolve()
    else:
        config["video_source_dir_abs"] = video_dir.resolve()

    log_d = Path(config.get("log_dir", "./logs"))
    if not log_d.is_absolute():
        config["log_dir_abs"] = (SCRIPT_DIR / log_d).resolve()
    else:
        config["log_dir_abs"] = log_d.resolve()

    return config


# ============================================================
# 模块1：设备检测
# ============================================================

class DeviceManager:
    """iOS 设备检测与管理"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def detect_devices(self) -> List[Dict]:
        devices = []
        try:
            result = subprocess.run(
                [_find_tool("idevice_id"), "-l"],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                self.logger.warning(f"idevice_id 返回异常: {result.stderr.strip()}")
                return []

            udid_list = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

            for udid in udid_list:
                device_info = self._get_device_info(udid)
                if device_info:
                    devices.append(device_info)
                    self.logger.info(f"检测到设备: {device_info['name']} ({udid[:8]}...)")

        except FileNotFoundError:
            from platform_utils import install_hint
            self.logger.error(install_hint())
        except Exception as e:
            self.logger.error(f"设备检测出错: {e}")

        return devices

    def _get_device_info(self, udid: str) -> Optional[Dict]:
        try:
            result = subprocess.run(
                [_find_tool("ideviceinfo"), "-u", udid, "-k", "DeviceName"],
                capture_output=True,
                text=True,
                timeout=15
            )
            if result.returncode == 0 and result.stdout.strip():
                name = result.stdout.strip()
                return {"udid": udid, "name": name}
        except subprocess.TimeoutExpired:
            self.logger.warning(f"获取设备信息超时: {udid[:8]}...")
        except Exception as e:
            self.logger.debug(f"获取 {udid[:8]}... 信息失败: {e}")
        return {"udid": udid, "name": f"Unknown-{udid[:8]}"}


# ============================================================
# 模块2：视频分配
# ============================================================

class VideoAllocator:
    """视频分配管理器"""

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.source_dir = config.get("video_source_dir_abs", SCRIPT_DIR / "videos_ready")
        if isinstance(self.source_dir, str):
            self.source_dir = Path(self.source_dir)
        self.extensions = config.get("video_extensions", [".mp4", ".mov", ".m4v"])
        self.videos_per_device = config.get("videos_per_device", 3)
        self.history = self._load_history()

    def _load_history(self) -> dict:
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                self.logger.warning(f"读取历史记录失败: {e}")
        return {"distributed_files": [], "sessions": []}

    def _save_history(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except IOError as e:
            self.logger.error(f"保存历史记录失败: {e}")

    def get_available_videos(self) -> List[Path]:
        if not self.source_dir.exists():
            self.logger.error(f"视频源目录不存在: {self.source_dir}")
            return []

        distributed_set = set(self.history.get("distributed_files", []))
        videos = []
        for ext in self.extensions:
            for f in self.source_dir.glob(f"*{ext}"):
                if f.name.startswith(".") or f.name.startswith("~"):
                    continue
                # 存文件名而非路径，换目录后仍可复用
                if f.name not in distributed_set:
                    videos.append(f)
        videos.sort(key=lambda p: p.stat().st_mtime)
        return videos

    def _extract_content_name(self, filename: str) -> str:
        """从文件名中提取内容名（用于区分不同内容）

        规则（按优先级）：
        1. 剥离尾部 `(数字)` 标记 → 空格+括号数字 是同一内容的不同版本
           如 `学生哥 (1).mp4` → `学生哥`
        2. 按 `-` 分割，取前半部分
           如 `CUT-(42).mp4` → `CUT`
           如 `办公室-1_1-转换.mp4` → `办公室`
        3. 无 `-` 也没括号 → 整个 stem
        """
        stem = filename.rsplit('.', 1)[0]  # 去掉扩展名

        # 剥离尾部 (数字) 括号标记
        import re
        stem = re.sub(r'\s*\(\d+\)\s*$', '', stem).strip()

        # 按 '-' 分割取前半部分
        if '-' in stem:
            return stem.split('-', 1)[0].strip()

        # 没有横线，整个 stem 就是内容名
        return stem

    def allocate(self, devices: List[Dict]) -> Dict[str, List[Path]]:
        available = self.get_available_videos()
        total_needed = len(devices) * self.videos_per_device

        self.logger.info(f"可用视频: {len(available)} 个 | 设备: {len(devices)} 台 | 需求: {total_needed} 个")

        if len(available) < total_needed:
            self.logger.warning(f"⚠️ 视频不足！需要 {total_needed} 个，但只有 {len(available)} 个可分配。")

        # ====== 按内容名分组 ======
        content_groups: Dict[str, List[Path]] = {}
        for video_path in available:
            content_name = self._extract_content_name(video_path.name)
            if content_name not in content_groups:
                content_groups[content_name] = []
            content_groups[content_name].append(video_path)

        unique_contents = sorted(content_groups.keys())
        self.logger.info(
            f"📦 内容分组: {len(unique_contents)} 个不同内容, "
            f"每台设备需 {self.videos_per_device} 个不同内容"
        )

        # ====== 轮询分配：保证每台设备拿到的视频来自不同内容 ======
        allocation: Dict[str, List[Path]] = {}
        used_videos: set = set()  # 已分配的视频路径（全局，避免跨设备重复）

        for i, device in enumerate(devices):
            udid = device["udid"]
            device_videos: List[Path] = []
            used_contents: set = set()  # ★ 当前设备已使用的内容名（防止同设备重复）

            # 从每个内容组轮流取一个，保证不同内容
            # 使用偏移量 i 让不同设备从不同位置开始取，增加多样性
            for j in range(self.videos_per_device):
                assigned = False

                # 遍历所有内容组，从 (i+j) 位置开始轮询
                for k in range(len(unique_contents)):
                    content_idx = (i + j + k) % len(unique_contents)
                    content_name = unique_contents[content_idx]

                    # ★ 跳过当前设备已经用过的内容组
                    if content_name in used_contents:
                        continue

                    candidates = content_groups[content_name]

                    for candidate in candidates:
                        if str(candidate) not in used_videos:
                            device_videos.append(candidate)
                            used_videos.add(str(candidate))
                            used_contents.add(content_name)  # ★ 标记该内容已被此设备使用
                            assigned = True
                            break

                    if assigned:
                        break

            if device_videos:
                allocation[udid] = device_videos
                # 显示分配结果（含内容名）
                detail = ", ".join(
                    f"{v.name} [{self._extract_content_name(v.name)}]"
                    for v in device_videos
                )
                contents_used = [self._extract_content_name(v.name) for v in device_videos]
                all_unique = len(contents_used) == len(set(contents_used))
                status = "✅ 不同内容" if all_unique else "⚠️ 有重复"
                self.logger.info(f"  → [{device['name']}] 分配 {len(device_videos)} 个视频 ({status}): {detail}")
            else:
                self.logger.warning(f"  → [{device['name']}] ⚠️ 无可用视频可分配！")

        return allocation

    def mark_as_distributed(self, allocations: Dict[str, List[Path]], success_map: Dict[str, bool]):
        """标记已分发文件

        如果 auto_delete_after_upload 为 false（保留文件模式），
        则跳过标记，下次分发还可以使用相同文件。
        """
        # 保留文件模式 = 不标记已分发（文件可重复使用）
        if not self.config.get("auto_delete_after_upload", True):
            self.logger.info("📌 保留文件模式：跳过标记已分发，视频可重复使用")
            return

        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_record = {
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "devices_count": len(allocations),
            "details": {}
        }

        new_distributed = []

        for udid, video_paths in allocations.items():
            success = success_map.get(udid, False)
            if success:
                for vp in video_paths:
                    fname = vp.name  # 存文件名而非路径
                    if fname not in self.history["distributed_files"]:
                        self.history["distributed_files"].append(fname)
                        new_distributed.append(fname)

            filenames = [vp.name for vp in video_paths]
            session_record["details"][udid] = {
                "videos": filenames,
                "success": success
            }

        self.history["sessions"].append(session_record)
        self._save_history()

        if new_distributed:
            self.logger.info(f"✅ 本次新增标记为已分发: {len(new_distributed)} 个视频")



# ============================================================
# 模块3：自动清理
# ============================================================

class FileCleaner:
    """文件清理管理器"""

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.auto_delete = config.get("auto_delete_after_upload", True)
        self.keep_failed = config.get("keep_failed_videos", True)

    def clean_uploaded_files(self, allocations: Dict[str, List[Path]],
                              success_map: Dict[str, bool], dry_run: bool = False):
        if not self.auto_delete:
            self.logger.info("自动清理已禁用，跳过删除操作")
            return

        deleted_count = 0
        kept_count = 0

        for udid, video_paths in allocations.items():
            success = success_map.get(udid, False)

            if success or not self.keep_failed:
                for vp in video_paths:
                    if dry_run:
                        self.logger.info(f"[DRY-RUN] 将删除: {vp.name}")
                        deleted_count += 1
                    else:
                        try:
                            vp.unlink()
                            self.logger.info(f"  🗑 已删除: {vp.name}")
                            deleted_count += 1
                        except OSError as e:
                            self.logger.warning(f"  删除失败: {vp.name} - {e}")
                            kept_count += 1
            else:
                kept_count += len(video_paths)
                for vp in video_paths:
                    self.logger.info(f"  🔒 保留（传输失败）: {vp.name}")

        self.logger.info(f"🧹 清理完成: 删除 {deleted_count} 个, 保留 {kept_count} 个")


# ============================================================
# 模块4：手机相册清理
# ============================================================

class PhotoCleaner:
    """iPhone 相册视频清理管理器"""

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def clear_old_videos(self, udid: str, device_name: str = "") -> int:
        """
        清空指定设备的 DCIM 旧视频

        Returns:
            int: 删除的文件数量
        """
        from clear_iphone_photos import clear_device_videos_sync

        self.logger.info(
            f"🗑️ [{device_name or udid[:8]}] 正在清空相机胶卷旧视频..."
        )

        result = clear_device_videos_sync(
            udid,
            media_types={'mp4', 'mov', 'm4v'}  # 只删视频，保留照片
        )

        deleted = result.get("deleted", 0)
        failed = result.get("failed", 0)
        error = result.get("error")

        if error:
            self.logger.warning(
                f"  ⚠️ [{device_name or udid[:8]}] "
                f"清空出错: {error}"
            )
            return 0

        if deleted > 0:
            self.logger.info(
                f"  ✅ [{device_name or udid[:8]}] "
                f"已删除 {deleted} 个旧视频"
            )
        else:
            self.logger.info(
                f"  ℹ️ [{device_name or udid[:8]}] "
                f"无旧视频需要删除"
            )

        if failed > 0:
            self.logger.warning(
                f"  ⚠️ 删除失败: {failed} 个文件"
            )

        return deleted

    def rebuild_all(self, devices: list) -> bool:
        """
        批量重建所有设备的照片库

        步骤：移除 Photos.sqlite → 重启设备

        Args:
            devices: [{"udid": str, "name": str}, ...]

        Returns:
            bool: 是否全部成功
        """
        from import_to_iphone import remove_photos_database, restart_device

        all_ok = True

        # 步骤1：移除数据库
        self.logger.info("📸 批量移除 Photos.sqlite...")
        for device in devices:
            udid = device["udid"]
            name = device.get("name", udid[:8])
            if remove_photos_database(udid):
                self.logger.info(f"  📝 [{name}] 数据库已移除")
            else:
                self.logger.warning(f"  ⚠️ [{name}] 移除失败")
                all_ok = False

        # 步骤2：重启
        self.logger.info("🔄 批量重启设备...")
        for device in devices:
            udid = device["udid"]
            name = device.get("name", udid[:8])
            if restart_device(udid):
                self.logger.info(f"  🔄 [{name}] 重启命令已发送")
            else:
                self.logger.warning(f"  ⚠️ [{name}] 重启失败")
                all_ok = False

        return all_ok
