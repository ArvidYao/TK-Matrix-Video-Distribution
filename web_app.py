#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TK 视频分发工具 - Web 可视化管理界面
=====================================
Flask 后端 API 服务，提供 RESTful API 和 SSE 实时进度推送

使用: python3 web_app.py
访问: http://localhost:5000
"""

import os
import sys
import json
import yaml
import time
import shutil
import re
import glob
import threading
import queue
import logging
import subprocess
import uuid

logger = logging.getLogger("TKDistributor.Web")

# 跨平台工具
from platform_utils import (create_process_group, kill_process_group,
                            get_yt_dlp_install_hint, check_yt_dlp_available)

# TikTok 去水印模块
from tiktok_watermark_remover import (
    get_clean_video_url, fetch_video_info, check_api_health,
    TikTokAPIError, VideoNotFoundError, RateLimitError,
)

from datetime import datetime, date
from pathlib import Path
from flask import Flask, request, jsonify, render_template, Response, send_file

# 确保项目根目录在路径中
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# 导入核心模块
from tk_distributor_core import (
    load_config, CONFIG_FILE, LOG_DIR, HISTORY_FILE,
    DeviceManager, VideoAllocator, FileCleaner,
)
import import_to_iphone
import clear_iphone_photos
import captions_manager
import airdrop_manager
import download_manager

# 导入 TikTok 爬虫模块
try:
    import tiktok_scraper
    from browser_engine import BrowserEngine
    HAS_TIKTOK_SCRAPER = True
    AVAILABLE_ENGINES = BrowserEngine.list_engines()
except ImportError:
    HAS_TIKTOK_SCRAPER = False
    AVAILABLE_ENGINES = []

# ============================================================
# 工具函数：实时监测设备重启上线
# ============================================================
def _wait_devices_online(expected_udids, emit_progress, logger, label="", poll_interval=3, max_wait=90):
    """轮询 idevice_id -l，等待所有设备重启后重新上线。

    分两个阶段：
      1. 等待设备断开（确认重启正在进行中）
      2. 等待设备重新上线（重启完成）

    Args:
        expected_udids: 期望上线的设备 UDID 列表
        emit_progress: SSE 进度回调
        logger: 日志记录器
        label: 可选的批次标签（如 "第1批"）
        poll_interval: 轮询间隔（秒），默认 3 秒
        max_wait: 最长等待时间（秒），默认 90 秒
    """
    if not expected_udids:
        return

    expected_set = set(expected_udids)
    total = len(expected_set)
    prefix = f"[{label}] " if label else ""

    def _get_online():
        try:
            result = subprocess.run(
                ["idevice_id", "-l"],
                capture_output=True, text=True, timeout=10
            )
            return set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
        except Exception:
            return set()

    started_at = time.time()

    # ── 阶段 1：等待设备断开（确认重启正在进行）──
    logger.info(f"{prefix}⏳ 等待设备断开（确认重启中）...")
    disconnect_detected = False
    while time.time() - started_at < max_wait:
        elapsed = time.time() - started_at
        online_udids = _get_online()
        still_on = expected_set & online_udids
        count_online = len(still_on)

        emit_progress("restart_waiting", {
            "message": f"{prefix}等待设备断开... 仍在线 {count_online}/{total}（{int(elapsed)}s/{max_wait}s）",
            "wait_seconds": int(max_wait - elapsed),
            "device_count": total,
            "online_count": count_online
        })

        if count_online < total:  # 至少有一台断开了
            disconnect_detected = True
            logger.info(f"{prefix}🔌 检测到设备断开：{count_online}/{total} 仍在线，重启中...")
            break

        if elapsed < 8:
            logger.info(f"{prefix}⏳ 刚发送重启命令，等待设备响应...")
        time.sleep(poll_interval)

    # ── 阶段 2：等待设备全部重新上线 ──
    if disconnect_detected:
        logger.info(f"{prefix}⏳ 等待设备全部重新上线...")
    while time.time() - started_at < max_wait:
        elapsed = time.time() - started_at
        online_udids = _get_online()
        back_online = expected_set & online_udids
        count_online = len(back_online)

        remaining = int(max_wait - elapsed)
        emit_progress("restart_waiting", {
            "message": f"{prefix}设备重启中... 已上线 {count_online}/{total}（{int(elapsed)}s/{max_wait}s）",
            "wait_seconds": remaining,
            "device_count": total,
            "online_count": count_online
        })

        if count_online >= total:
            logger.info(f"{prefix}✅ 全部 {total} 台设备已上线（耗时 {int(elapsed)} 秒）")
            emit_progress("restart_waiting", {
                "message": f"{prefix}全部 {total} 台设备已上线（{int(elapsed)}s）",
                "wait_seconds": 0,
                "device_count": total,
                "online_count": total
            })
            return

        logger.info(f"{prefix}⏳ 已上线 {count_online}/{total}，等待 {poll_interval}s 后重试...")
        time.sleep(poll_interval)

    # 超时
    elapsed = time.time() - started_at
    logger.info(f"{prefix}⏰ 已等待 {int(elapsed)} 秒（达上限），继续执行")
    emit_progress("restart_waiting", {
        "message": f"{prefix}等待 {int(elapsed)} 秒（达上限），继续...",
        "wait_seconds": 0,
        "device_count": total
    })


app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ============================================================
# 全局状态管理
# ============================================================

class AppState:
    """应用全局状态（线程安全）"""

    def __init__(self):
        self._lock = threading.Lock()
        self.is_running = False
        self.should_stop = False
        self.progress_queue = queue.Queue()  # SSE 消息队列
        self.current_task_info = {}
        self.config = None
        self.current_task_id = None  # 当前任务唯一ID
        self._load_config()

    def clear_queue(self):
        """清空消息队列（任务开始前调用）"""
        while not self.progress_queue.empty():
            try:
                self.progress_queue.get_nowait()
            except queue.Empty:
                break

    def _load_config(self):
        with self._lock:
            self.config = load_config()

    def reload_config(self):
        with self._lock:
            self.config = load_config()

    def get_config_copy(self):
        with self._lock:
            return dict(self.config)

state = AppState()

# ============================================================
# TikTok 爬虫状态
# ============================================================

class ScraperState:
    """TikTok 爬虫全局状态"""
    def __init__(self):
        self._lock = threading.Lock()
        self.is_running = False
        self.should_stop = False
        self.progress_queue = queue.Queue()
        self.current_task_id = None
        self.last_results = []       # 上次爬取全量结果
        self.last_qualified = []     # 上次爬取符合条件结果

scraper_state = ScraperState()


def emit_scraper_progress(event_type, data=None):
    """向 SSE 客户端推送爬虫进度"""
    msg = {"type": event_type, "timestamp": datetime.now().isoformat(), "task_id": scraper_state.current_task_id}
    if data:
        msg.update(data)
    scraper_state.progress_queue.put(msg)


def emit_progress(event_type, data=None):
    """向所有 SSE 客户端推送进度消息"""
    msg = {"type": event_type, "timestamp": datetime.now().isoformat(), "task_id": state.current_task_id}
    if data:
        msg.update(data)
    state.progress_queue.put(msg)


# ============================================================
# 工具函数
# ============================================================

def get_video_dir():
    """获取当前视频源目录"""
    cfg = state.get_config_copy()
    return Path(cfg.get("video_source_dir_abs", str(SCRIPT_DIR / "videos_ready")))


def format_size(size_bytes):
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def scan_videos(video_dir: Path) -> list:
    """扫描待分发视频目录"""
    if not video_dir.exists():
        return []
    videos = []
    extensions = [".mp4", ".mov", ".m4v"]
    for ext in extensions:
        for f in video_dir.glob(f"*{ext}"):
            if not f.name.startswith(".") and not f.name.startswith("~"):
                stat = f.stat()
                videos.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "size_human": format_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "path": str(f)
                })
    # 按修改时间排序
    videos.sort(key=lambda v: v["modified"])
    return videos


def load_history_data() -> dict:
    """加载分发历史"""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"distributed_files": [], "sessions": []}


# ============================================================
# API 路由
# ============================================================

@app.route("/")
def index():
    """主页 - 返回单页应用 HTML"""
    return render_template("index.html")


@app.route("/ico.png")
@app.route("/favicon.ico")
def favicon():
    """返回程序图标"""
    ico_path = SCRIPT_DIR / "ICO.png"
    if ico_path.exists():
        return send_file(str(ico_path), mimetype="image/png")
    return "", 404


# ---- 系统状态 ----

@app.route("/api/status")
def api_status():
    """获取系统状态概览"""
    cfg = state.get_config_copy()
    video_dir = get_video_dir()

    # 扫描视频
    videos = scan_videos(video_dir)

    # 统计今日数据
    history = load_history_data()
    today_str = date.today().isoformat()
    today_sessions = [
        s for s in history.get("sessions", [])
        if s.get("timestamp", "").startswith(today_str)
    ]
    today_total_devices = sum(s.get("devices_count", 0) for s in today_sessions)
    today_success_count = sum(
        1 for s in today_sessions
        for d in s.get("details", {}).values()
        if d.get("success")
    )
    today_fail_count = sum(
        1 for s in today_sessions
        for d in s.get("details", {}).values()
        if not d.get("success")
    )

    total_sessions = len(history.get("sessions", []))
    total_distributed = len(history.get("distributed_files", []))

    return jsonify({
        "running": state.is_running,
        "video_dir": str(video_dir),
        "video_dir_exists": video_dir.exists(),
        "video_count": len(videos),
        "video_total_size": sum(v["size"] for v in videos),
        "video_total_size_human": format_size(sum(v["size"] for v in videos)),
        "today_sessions": len(today_sessions),
        "today_devices": today_total_devices,
        "today_success": today_success_count,
        "today_fail": today_fail_count,
        "total_sessions": total_sessions,
        "total_distributed": total_distributed,
        "config": {
            "videos_per_device": cfg.get("videos_per_device", 3),
            "auto_delete": cfg.get("auto_delete_after_upload", True),
            "max_batch": cfg.get("max_devices_per_batch", 20),
        }
    })


# ---- 设置管理 ----

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    """获取当前设置"""
    cfg = state.get_config_copy()
    return jsonify({
        "video_source_dir": cfg.get("video_source_dir", "./videos_ready"),
        "videos_per_device": cfg.get("videos_per_device", 3),
        "auto_delete_after_upload": cfg.get("auto_delete_after_upload", True),
        "keep_failed_videos": cfg.get("keep_failed_videos", True),
        "clear_before_upload": cfg.get("clear_before_upload", False),
        "max_devices_per_batch": cfg.get("max_devices_per_batch", 20),
        "phone_target_path": cfg.get("phone_target_path", "DCIM/100APPLE"),
        "dry_run": cfg.get("dry_run", False),
    })


@app.route("/api/settings", methods=["PUT"])
def api_update_settings():
    """更新设置并保存到 config.yaml"""
    try:
        data = request.json
        if not CONFIG_FILE.exists():
            return jsonify({"error": "配置文件不存在"}), 400

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        # 更新字段
        field_map = {
            "video_source_dir": "video_source_dir",
            "videos_per_device": "videos_per_device",
            "auto_delete_after_upload": "auto_delete_after_upload",
            "keep_failed_videos": "keep_failed_videos",
            "clear_before_upload": "clear_before_upload",
            "max_devices_per_batch": "max_devices_per_batch",
            "phone_target_path": "phone_target_path",
            "dry_run": "dry_run",
        }

        for key, yaml_key in field_map.items():
            if key in data:
                config[yaml_key] = data[key]

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        # 重新加载配置到内存
        state.reload_config()

        app.logger.info(f"设置已更新: {data}")
        return jsonify({"success": True, "message": "设置已保存"})

    except Exception as e:
        app.logger.error(f"更新设置失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/history/reset", methods=["POST"])
def api_reset_history():
    """重置分发历史记录（清空已分发标记，所有视频重新变为可用）"""
    try:
        if HISTORY_FILE.exists():
            # 先读取做备份记录
            old_data = load_history_data()
            cleared_count = len(old_data.get("distributed_files", []))
            sessions_count = len(old_data.get("sessions", []))

            # 写入空记录
            HISTORY_FILE.write_text(json.dumps({
                "distributed_files": [],
                "sessions": []
            }, ensure_ascii=False, indent=2))

            app.logger.info(f"历史记录已重置: 清除 {cleared_count} 个已分发标记, {sessions_count} 条会话记录")
            return jsonify({
                "success": True,
                "message": f"已重置（释放 {cleared_count} 个视频，清除 {sessions_count} 条历史）",
                "cleared_videos": cleared_count,
                "cleared_sessions": sessions_count
            })
        else:
            return jsonify({"success": True, "message": "没有需要重置的历史数据"})

    except Exception as e:
        app.logger.error(f"重置历史失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/choose-dir", methods=["POST"])
def api_choose_directory():
    """通过独立子进程弹出系统文件夹选择对话框"""
    # 结果文件路径（每个请求唯一，避免并发冲突）
    result_file = Path(f"/tmp/tk_chooser_{int(time.time()*1000)}.json")

    # 清理可能存在的旧结果
    if result_file.exists():
        result_file.unlink()

    # 启动独立子进程来弹窗（不阻塞 Flask 主线程的事件循环）
    tool_script = SCRIPT_DIR / "choose_dir_tool.py"
    proc = subprocess.Popen(
        [sys.executable, str(tool_script), str(result_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **create_process_group()  # 跨平台：脱离父进程组，获得独立 GUI 权限
    )

    # 轮询等待结果文件出现（最长等 120 秒）
    timeout = 120
    start = time.time()
    while time.time() - start < timeout:
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
                result_file.unlink(missing_ok=True)
                return jsonify(data)
            except (json.JSONDecodeError, IOError):
                pass
        # 检查进程是否已退出（出错情况）
        if proc.poll() is not None:
            break
        time.sleep(0.3)

    # 超时或异常
    if proc.poll() is None:
        proc.terminate()
    return jsonify({"success": False, "error": "操作超时或被取消"}), 408


# ---- 设备检测 ----

@app.route("/api/devices")
def api_devices():
    """扫描并返回已连接的 iOS 设备"""
    logger = logging.getLogger(__name__)
    device_manager = DeviceManager(logger)
    devices = device_manager.detect_devices()

    result = []
    for d in devices:
        result.append({
            "udid": d["udid"],
            "name": d["name"],
            "status": "connected"
        })

    return jsonify({"devices": result, "count": len(result)})


# ---- 设备视频清理 ----

@app.route("/api/devices/clear-videos", methods=["POST"])
def api_clear_device_videos():
    """清空 iPhone DCIM 目录中的视频文件"""
    data = request.json or {}
    media_type = data.get("media_type", "mp4_mov")
    dry_run = data.get("dry_run", False)

    # 确定媒体类型集合
    if media_type == "all":
        media_types = None  # clear_iphone_photos 会用默认的所有媒体类型
    else:
        media_types = {"mp4", "mov", "m4v"}

    # 获取设备列表
    logger = logging.getLogger(__name__)
    device_manager = DeviceManager(logger)
    devices = device_manager.detect_devices()

    if not devices:
        return jsonify({"success": False, "error": "未检测到设备，请先连接 iPhone"}), 400

    # 对每台设备执行清理
    all_results = []
    for device in devices:
        udid = device["udid"]
        name = device["name"]
        try:
            result = clear_iphone_photos.clear_device_videos_sync(
                udid, media_types=media_types, dry_run=dry_run
            )
            result["udid"] = udid[:8] + "..."
            result["name"] = name
            all_results.append(result)
        except Exception as e:
            app.logger.error(f"清理设备 {name} 失败: {e}")
            all_results.append({
                "udid": udid[:8] + "...",
                "name": name,
                "deleted": 0,
                "failed": 1,
                "files": [],
                "error": str(e)
            })

    # 汇总结果
    total_deleted = sum(r.get("deleted", 0) for r in all_results)
    total_failed = sum(r.get("failed", 0) for r in all_results)
    has_errors = any(r.get("error") for r in all_results)

    return jsonify({
        "success": True,
        "deleted_count": total_deleted,
        "failed_count": total_failed,
        "dry_run": dry_run,
        "udid": devices[0]["udid"][:8] + "..." if devices else "",
        "device_count": len(devices),
        "per_device_results": all_results,
        "message": f"共处理 {len(devices)} 台设备，删除 {total_deleted} 个文件，失败 {total_failed}"
    })


# ---- 视频管理 ----

@app.route("/api/videos")
def api_videos():
    """获取待分发视频列表"""
    video_dir = get_video_dir()
    videos = scan_videos(video_dir)
    return jsonify({
        "videos": videos,
        "count": len(videos),
        "dir": str(video_dir),
        "exists": video_dir.exists()
    })


@app.route("/api/videos/<filename>", methods=["DELETE"])
def api_delete_video(filename):
    """删除指定视频文件"""
    # 安全检查：防止路径遍历攻击
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        return jsonify({"error": "无效的文件名"}), 400

    video_dir = get_video_dir()
    target = video_dir / safe_name

    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404

    try:
        target.unlink()
        app.logger.info(f"已删除视频: {safe_name}")
        return jsonify({"success": True, "message": f"已删除 {safe_name}"})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


# ---- 分发任务 ----

@app.route("/api/distribute/start", methods=["POST"])
def api_distribute_start():
    """启动分发任务（后台运行）"""
    if state.is_running:
        return jsonify({"error": "任务正在运行中，请先停止当前任务"}), 400

    data = request.json or {}
    dry_run = data.get("dry_run", False)

    # 生成新任务ID，清空旧消息队列
    import uuid
    task_id = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
    state.current_task_id = task_id
    state.clear_queue()

    # 启动后台线程执行分发
    thread = threading.Thread(target=run_distribution_task, args=(dry_run,), daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "message": "分发任务已启动",
        "dry_run": dry_run,
        "task_id": task_id
    })


@app.route("/api/distribute/stop", methods=["POST"])
def api_distribute_stop():
    """停止正在运行的分发任务"""
    if not state.is_running:
        return jsonify({"error": "没有正在运行的任务"}), 400

    state.should_stop = True
    return jsonify({"success": True, "message": "已发送停止信号"})


@app.route("/api/distribute/progress")
def api_progress_stream():
    """SSE 实时进度流"""
    def generate():
        # 发送初始连接成功消息
        yield f"data: {json.dumps({'type': 'connected', 'timestamp': datetime.now().isoformat()})}\n\n"

        while True:
            try:
                msg = state.progress_queue.get(timeout=30)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except queue.Empty:
                # 发送心跳保持连接
                yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.now().isoformat()})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ---- 历史记录 ----

@app.route("/api/history")
def api_history():
    """获取分发历史记录"""
    history = load_history_data()
    sessions = history.get("sessions", [])

    # 反转顺序（最新的在前）
    sessions.reverse()

    # 为每条记录添加汇总信息
    enriched = []
    for s in sessions:
        details = s.get("details", {})
        success_count = sum(1 for d in details.values() if d.get("success"))
        fail_count = len(details) - success_count
        video_count = sum(len(d.get("videos", [])) for d in details.values())

        enriched.append({
            "session_id": s.get("session_id"),
            "timestamp": s.get("timestamp"),
            "devices_count": s.get("devices_count", 0),
            "video_count": video_count,
            "success_count": success_count,
            "fail_count": fail_count,
            "details": details,
        })

    return jsonify({
        "sessions": enriched,
        "total": len(enriched),
        "total_distributed_files": len(history.get("distributed_files", []))
    })


# ---- 强制重建相册索引（解决 AFC 上传后照片 App 不显示的问题）----

@app.route("/api/photos/rescan", methods=["POST"])
def api_photos_rescan():
    """
    手动触发 iPhone 照片库索引重建。

    原理：通过重命名 Photos.sqlite 触发 iOS 重新扫描 DCIM 目录，
    使通过 AFC 上传的视频文件出现在照片 App 中。
    """
    data = request.json or {}
    udid = data.get("udid")

    if not udid:
        # 如果没有指定 udid，自动检测当前连接的设备
        logger = logging.getLogger(__name__)
        device_manager = DeviceManager(logger)
        devices = device_manager.detect_devices()
        if not devices:
            return jsonify({"error": "未检测到设备，请先连接 iPhone"}), 400
        udid = devices[0]["udid"]

    try:
        # 改用 rebuild_and_restart（改名数据库 + 重启 iPhone）
        # 仅发通知或改名不够，必须重启才能触发 iOS 重新扫描
        import_to_iphone.rebuild_and_restart(udid)

        return jsonify({
            "success": True,
            "message": f"已对设备 {udid[:8]}... 发起数据库重建 + 重启，请等待 60-90 秒后打开 iPhone 照片 App 查看",
            "udid": udid[:8] + "..."
        })
    except Exception as e:
        app.logger.error(f"照片库重建失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/photos/reboot-rebuild", methods=["POST"])
def api_photos_reboot_rebuild():
    """
    终极重建：移除 Photos.sqlite + 重启 iPhone

    这是目前唯一验证过能让 AFC 上传的视频出现在相册的方式。
    重启后 iOS 会自动扫描 DCIM 目录重建照片库。
    """
    data = request.json or {}
    udid = data.get("udid")

    if not udid:
        logger = logging.getLogger(__name__)
        device_manager = DeviceManager(logger)
        devices = device_manager.detect_devices()
        if not devices:
            return jsonify({"error": "未检测到设备"}), 400
        udid = devices[0]["udid"]

    try:
        import_to_iphone.rebuild_and_restart(udid)
        return jsonify({
            "success": True,
            "message": f"已对设备 {udid[:8]}... 执行终极重建（移除 Photos.sqlite + 重启），请等待 60-90 秒",
            "udid": udid[:8] + "..."
        })
    except Exception as e:
        app.logger.error(f"终极重建失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ---- 文案管理 ----

@app.route("/api/captions", methods=["GET"])
def api_list_captions():
    """获取所有文案"""
    try:
        # 首次加载时初始化默认文案
        captions_manager.init_default_captions()
        captions = captions_manager.list_captions()
        return jsonify({"captions": captions, "count": len(captions)})
    except Exception as e:
        app.logger.error(f"获取文案列表失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/captions", methods=["POST"])
def api_add_caption():
    """添加新文案"""
    try:
        data = request.json or {}
        name = data.get("name", "").strip()
        content = data.get("content", "").strip()

        if not name:
            return jsonify({"error": "文案名称不能为空"}), 400
        if not content:
            return jsonify({"error": "文案内容不能为空"}), 400

        caption = captions_manager.add_caption(name, content)
        app.logger.info(f"已添加文案: {name}")
        return jsonify({"success": True, "caption": caption})
    except Exception as e:
        app.logger.error(f"添加文案失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/captions/<caption_id>", methods=["PUT"])
def api_update_caption(caption_id):
    """更新文案"""
    try:
        data = request.json or {}
        name = data.get("name")
        content = data.get("content")

        caption = captions_manager.update_caption(caption_id, name=name, content=content)
        if not caption:
            return jsonify({"error": "文案不存在"}), 404

        app.logger.info(f"已更新文案: {caption.get('name')}")
        return jsonify({"success": True, "caption": caption})
    except Exception as e:
        app.logger.error(f"更新文案失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/captions/<caption_id>", methods=["DELETE"])
def api_delete_caption(caption_id):
    """删除文案"""
    try:
        deleted = captions_manager.delete_caption(caption_id)
        if not deleted:
            return jsonify({"error": "文案不存在"}), 404

        app.logger.info(f"已删除文案: {caption_id}")
        return jsonify({"success": True, "message": "文案已删除"})
    except Exception as e:
        app.logger.error(f"删除文案失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/captions/send", methods=["POST"])
def api_send_caption():
    """
    通过 AirDrop 发送文案（无需 USB，纯无线传输）

    请求体:
    {
        "caption_id": "文案ID"
    }
    """
    try:
        data = request.json or {}
        caption_id = data.get("caption_id")

        if not caption_id:
            return jsonify({"error": "请选择一条文案"}), 400

        caption = captions_manager.get_caption(caption_id)
        if not caption:
            return jsonify({"error": "文案不存在"}), 404

        caption_name = caption.get("name", "TikTok文案")
        caption_text = caption.get("content", "")

        # 调起 AirDrop 发送（弹出 macOS 共享面板，用户手动选择设备）
        temp_file = airdrop_manager.send_caption_via_airdrop(caption_text, caption_name)

        app.logger.info(f"AirDrop 文案「{caption_name}」已调起共享面板")

        return jsonify({
            "success": True,
            "message": f"已调起 AirDrop 发送「{caption_name}」，请在 AirDrop 面板中点击目标设备",
            "caption_name": caption_name,
            "temp_file": temp_file,
        })
    except Exception as e:
        app.logger.error(f"发送文案失败: {e}")
        return jsonify({"error": str(e)}), 500


# ---- TikTok 热门视频爬虫 ----

@app.route("/api/scraper/status")
def api_scraper_status():
    """获取爬虫状态"""
    return jsonify({
        "available": HAS_TIKTOK_SCRAPER,
        "running": scraper_state.is_running,
        "last_results_count": len(scraper_state.last_results),
        "last_qualified_count": len(scraper_state.last_qualified),
        "engines": AVAILABLE_ENGINES,
    })


@app.route("/api/scraper/start", methods=["POST"])
def api_scraper_start():
    """启动 TikTok 爬取任务"""
    if not HAS_TIKTOK_SCRAPER:
        return jsonify({"error": "TikTok 爬虫模块未安装（缺少 browser_engine / playwright）"}), 400
    if scraper_state.is_running:
        return jsonify({"error": "爬虫任务正在运行中"}), 400

    data = request.json or {}
    hashtags = data.get("hashtags", tiktok_scraper.DEFAULT_HASHTAGS)
    ms_token = data.get("ms_token", "")
    proxy = data.get("proxy", "")
    min_plays = data.get("min_plays", 100000)
    min_duration = data.get("min_duration", 70)
    max_scrolls = data.get("max_scrolls", 5)
    engine_name = data.get("engine_name", "auto")  # 支持前端指定引擎

    # 生成任务 ID
    import uuid
    task_id = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
    scraper_state.current_task_id = task_id
    scraper_state.should_stop = False

    # 清空消息队列
    while not scraper_state.progress_queue.empty():
        try:
            scraper_state.progress_queue.get_nowait()
        except queue.Empty:
            break

    def _run():
        scraper_state.is_running = True
        start_time = time.time()

        # 设置爬虫日志文件（用于排查问题）
        scraper_log = logging.getLogger("TKScraper")
        scraper_log.setLevel(logging.DEBUG)
        log_dir = SCRIPT_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        scraper_log_file = log_dir / f"scraper_{date.today().isoformat()}.log"
        fh = logging.FileHandler(str(scraper_log_file), encoding="utf-8", mode="a")
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
        fh.setLevel(logging.DEBUG)
        # 避免重复添加 handler
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(scraper_log_file)
                   for h in scraper_log.handlers):
            scraper_log.addHandler(fh)
        # 也添加 Playwright/BrowserEngine 的日志
        for log_name in ["BrowserEngine", "PlaywrightEngine", "AgentBrowserEngine"]:
            l = logging.getLogger(log_name)
            l.setLevel(logging.DEBUG)
            if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(scraper_log_file)
                       for h in l.handlers):
                l.addHandler(fh)

        try:
            scraper = tiktok_scraper.TikTokScraper(
                ms_token=ms_token,
                proxy=proxy,
                hashtags=hashtags,
                min_plays=min_plays,
                min_duration=min_duration,
                max_scrolls=max_scrolls,
                on_progress=lambda et, d: emit_scraper_progress(et, d),
                should_stop_fn=lambda: scraper_state.should_stop,
                engine_name=engine_name,
            )
            results, qualified = scraper.run()
            elapsed = time.time() - start_time
            with scraper_state._lock:
                scraper_state.last_results = results
                scraper_state.last_qualified = qualified
            # ── 记录搜索会话到历史 ──
            _save_scraper_session(hashtags, len(results), len(qualified), round(elapsed, 1), task_id, engine_name)
            # 结果已保存，现在安全地发送完成事件
            emit_scraper_progress("complete", {
                "total": len(results),
                "qualified": len(qualified),
                "elapsed": round(elapsed, 1),
                "hashtags": hashtags,
            })
        except Exception as e:
            app.logger.error(f"爬虫任务异常: {e}", exc_info=True)
            # 确保即使出错，已有数据也保存到 state（让前端能加载）
            with scraper_state._lock:
                if not scraper_state.last_results:
                    scraper_state.last_results = []
                    scraper_state.last_qualified = []
            # 记录失败的搜索会话（total=0 表示异常退出）
            try:
                _save_scraper_session(hashtags, 0, 0, round(time.time() - start_time, 1), task_id, engine_name)
            except Exception:
                pass
            try:
                emit_scraper_progress("error", {"message": f"爬虫任务异常: {e}"})
            except Exception:
                pass
        finally:
            scraper_state.is_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "message": f"爬虫任务已启动（{len(hashtags)} 个 hashtag，引擎: {engine_name}）",
        "task_id": task_id,
        "hashtags": hashtags,
        "engine": engine_name,
    })


@app.route("/api/scraper/stop", methods=["POST"])
def api_scraper_stop():
    """停止爬虫任务"""
    if not scraper_state.is_running:
        return jsonify({"error": "没有正在运行的爬虫任务"}), 400
    scraper_state.should_stop = True

    # 启动看门狗：如果 5 秒后 is_running 还没被线程重置，强制重置
    def _force_reset():
        time.sleep(5)
        if scraper_state.is_running:
            scraper_state.is_running = False
            app.logger.warning("爬虫任务强制停止（线程可能已卡死）")

    threading.Thread(target=_force_reset, daemon=True).start()
    return jsonify({"success": True, "message": "已发送停止信号"})


@app.route("/api/scraper/progress")
def api_scraper_progress():
    """SSE 实时爬虫进度流"""
    def generate():
        yield f"data: {json.dumps({'type': 'connected', 'timestamp': datetime.now().isoformat()})}\n\n"
        while True:
            try:
                msg = scraper_state.progress_queue.get(timeout=30)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                # 任务结束后再发几条心跳确保客户端收到完成消息
                if msg.get("type") in ("complete", "error", "stopped"):
                    for _ in range(3):
                        time.sleep(0.5)
                        yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.now().isoformat()})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/scraper/results")
def api_scraper_results():
    """获取上次爬取结果"""
    with scraper_state._lock:
        result = {
            "total": len(scraper_state.last_results),
            "qualified": len(scraper_state.last_qualified),
            "results": scraper_state.last_qualified[:200],  # 最多返回 200 条
            "all_results": scraper_state.last_results[:500],
        }
    logger.info(f"API scraper/results: total={result['total']}, qualified={result['qualified']}")
    return jsonify(result)


# ── 视频下载管理 ──
# 下载目录统一由 download_manager 模块管理（支持持久化设置）
# 下载任务状态追踪 { video_id: {"status": "downloading"|"done"|"error", "filename": ..., "progress": ...} }
_download_tasks = {}
_download_lock = threading.Lock()


# ── 下载设置管理 API ──

@app.route("/api/download/settings", methods=["GET"])
def api_download_settings():
    """获取下载目录设置"""
    try:
        settings = download_manager.DownloadSettings()
        return jsonify({"success": True, **settings.get_settings_dict()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/download/settings", methods=["PUT"])
def api_download_settings_update():
    """更新下载目录设置（文本方式，无需弹窗）"""
    try:
        data = request.json or {}
        new_dir = data.get("download_dir", "").strip()

        if not new_dir:
            return jsonify({"success": False, "error": "下载目录不能为空"}), 400

        if not os.path.isabs(new_dir):
            new_dir = os.path.abspath(os.path.join(SCRIPT_DIR, new_dir))

        os.makedirs(new_dir, exist_ok=True)
        settings = download_manager.DownloadSettings()
        settings.set_download_dir(new_dir)

        app.logger.info(f"下载目录已更新为: {new_dir}")
        return jsonify({"success": True, **settings.get_settings_dict()})
    except PermissionError:
        return jsonify({"success": False, "error": "没有权限创建该目录"}), 403
    except Exception as e:
        app.logger.error(f"更新下载目录失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/download/settings", methods=["DELETE"])
def api_download_settings_reset():
    """重置下载目录为默认值"""
    try:
        settings = download_manager.DownloadSettings()
        settings.reset_to_default()
        return jsonify({"success": True, **settings.get_settings_dict()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/scraper/video/choose-dir", methods=["POST"])
def api_scraper_video_choose_dir():
    """通过系统对话框选择视频下载保存目录"""
    result_file = Path(f"/tmp/tk_scraper_chooser_{int(time.time()*1000)}.json")
    if result_file.exists():
        result_file.unlink()

    tool_script = SCRIPT_DIR / "choose_dir_tool.py"
    proc = subprocess.Popen(
        [sys.executable, str(tool_script), str(result_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **create_process_group()
    )

    timeout = 120
    start = time.time()
    while time.time() - start < timeout:
        if result_file.exists():
            try:
                result_data = json.loads(result_file.read_text())
                result_file.unlink(missing_ok=True)
                return jsonify(result_data)
            except (json.JSONDecodeError, IOError):
                pass
        if proc.poll() is not None:
            break
        time.sleep(0.3)

    if proc.poll() is None:
        proc.terminate()
    return jsonify({"success": False, "error": "操作超时或被取消"}), 408


@app.route("/api/scraper/video/download", methods=["POST"])
def api_scraper_video_download():
    """下载 TikTok 视频素材到本地，支持去水印模式，下载后自动重命名为「日期_标题.mp4」。

    请求 JSON:
        video_url:   TikTok 视频页面 URL
        video_id:    视频 ID（任意标识）
        save_dir:    保存目录（可选，不传则使用持久化设置或默认目录）
        watermark:   是否保留水印 (bool, 默认 true)
                       - true:  带水印下载（yt-dlp 默认格式）
                       - false: 去水印下载（通过 TikTok API 获取 clean_url）
        description: 视频标题/描述（用于下载后自动重命名，可选）
    """
    data = request.json or {}
    video_url = data.get("video_url", "")
    video_id = data.get("video_id", "")
    save_dir = data.get("save_dir", "")
    description = data.get("description", "")
    remove_watermark = not data.get("watermark", True)  # watermark=true 默认保留水印

    if not video_url:
        return jsonify({"success": False, "message": "缺少视频链接"}), 400

    # 使用 download_manager 统一获取下载目录
    output_dir = download_manager.get_effective_download_dir(save_dir)
    os.makedirs(output_dir, exist_ok=True)

    # 检查是否已有相同任务
    task_key = f"{video_id}::{output_dir}"
    with _download_lock:
        if task_key in _download_tasks and _download_tasks[task_key]["status"] == "downloading":
            return jsonify({"success": False, "message": "该视频正在下载中，请稍候"}), 409

    # 查找 yt-dlp
    ytdlp_path = shutil.which("yt-dlp") or shutil.which("ytdlp")

    if not ytdlp_path:
        hint = get_yt_dlp_install_hint()
        logger.warning("yt-dlp 未安装，返回安装指南给用户")
        return jsonify({
            "success": False,
            "message": hint,
            "error_type": "dependency_missing",
        }), 400

    # 标记任务状态
    with _download_lock:
        _download_tasks[task_key] = {
            "status": "downloading",
            "filename": None,
            "filepath": None,
            "error": None,
            "watermark_removed": remove_watermark,
        }

    def _do_download():
        download_url = video_url
        watermark_status = "with_watermark"
        use_direct_url = False
        api_title = description  # 标题来源：优先用户传入

        # ── 核心优化：始终先通过 TikTok API 解析直链 ──
        # 原因：yt-dlp 内置 TikTok 提取器因网页结构变化频繁失效，
        # 而 TikTok Feed API 返回的 CDN 直链可被 yt-dlp 直接下载。
        try:
            logger.info(f"🔍 解析 TikTok 视频直链 [{video_id}]")
            info = fetch_video_info(video_url)

            # 如果前端未传描述，从 API 获取
            if not api_title and info.get("desc"):
                api_title = info["desc"]
                logger.info(f"📝 从 TikTok API 获取标题: {api_title[:50]}...")

            if remove_watermark and info.get("clean_url"):
                # 用户要求去水印 + API 有无水印链接
                download_url = info["clean_url"]
                watermark_status = "no_watermark"
                use_direct_url = True
                logger.info(f"✓ 已获取无水印直链 [{video_id}]")
            elif info.get("watermark_url"):
                # 保留水印 / 无水印链接为空的降级
                download_url = info["watermark_url"]
                watermark_status = "with_watermark"
                use_direct_url = True
                logger.info(f"✓ 已获取直链（带水印） [{video_id}]")
            elif info.get("clean_url"):
                # watermark_url 为空时使用 clean_url
                download_url = info["clean_url"]
                watermark_status = "fallback_no_watermark"
                use_direct_url = True
                logger.info(f"✓ 已获取无水印直链（降级） [{video_id}]")
        except VideoNotFoundError as e:
            logger.warning(f"TikTok API 无结果 [{video_id}]: {e}，回退到 yt-dlp 网页解析")
            watermark_status = "fallback"
        except RateLimitError as e:
            logger.warning(f"TikTok API 限流 [{video_id}]: {e}，回退到 yt-dlp 网页解析")
            watermark_status = "rate_limited"
        except TikTokAPIError as e:
            logger.warning(f"TikTok API 调用失败 [{video_id}]: {e}，回退到 yt-dlp 网页解析")
            watermark_status = "api_error"

        # ── yt-dlp 下载 ──
        try:
            result = subprocess.run(
                [
                    ytdlp_path,
                    "-o", os.path.join(output_dir, "%(id)s.%(ext)s"),
                    "--no-playlist",
                    "--no-warnings",
                    "--max-filesize", "500M",
                    download_url,
                ],
                capture_output=True, text=True, timeout=180,
            )
            output = result.stdout + result.stderr

            if result.returncode == 0:
                # 查找下载的文件
                dest_match = re.search(r"Destination:\s*(.+)", output)
                if dest_match:
                    filepath = dest_match.group(1).strip()
                else:
                    files = sorted(
                        glob.glob(os.path.join(output_dir, "*")),
                        key=os.path.getmtime, reverse=True,
                    )
                    filepath = files[0] if files else None

                if filepath and os.path.exists(filepath):
                    # ── ★ 下载后自动重命名为「日期_标题.mp4」──
                    try:
                        # 如果 yt-dlp 本身返回了标题，优先使用
                        ytdlp_title = None
                        title_match = re.search(r'\[download\]\s+(.+?)\s+has already been downloaded|\[download\]\s+Destination:\s*(.+)', output)
                        # 使用 download_manager 重命名
                        rename_result = download_manager.rename_downloaded_video(
                            filepath=filepath,
                            title=api_title or video_id,
                        )
                        final_filepath = rename_result["filepath"]
                        final_filename = rename_result["filename"]
                        logger.info(f"✓ 视频已重命名 [{video_id}]: {final_filename}")
                    except Exception as rename_err:
                        # 重命名失败不阻止下载完成，使用原始文件名
                        logger.warning(f"重命名失败，保留原始文件名 [{video_id}]: {rename_err}")
                        final_filepath = filepath
                        final_filename = os.path.basename(filepath)

                    with _download_lock:
                        _download_tasks[task_key] = {
                            "status": "done",
                            "filename": final_filename,
                            "filepath": final_filepath,
                            "watermark_status": watermark_status,
                        }
                else:
                    with _download_lock:
                        _download_tasks[task_key] = {
                            "status": "error",
                            "error": "下载成功但未找到保存文件",
                            "watermark_status": watermark_status,
                        }
            else:
                err_msg = output[-300:] if len(output) > 300 else output
                with _download_lock:
                    _download_tasks[task_key] = {
                        "status": "error",
                        "error": err_msg,
                        "watermark_status": watermark_status,
                    }
                logger.error(f"视频下载失败 [{video_id}]: {err_msg}")
        except subprocess.TimeoutExpired:
            with _download_lock:
                _download_tasks[task_key] = {
                    "status": "error",
                    "error": "下载超时（180秒）",
                    "watermark_status": watermark_status,
                }
        except FileNotFoundError:
            hint = get_yt_dlp_install_hint()
            logger.warning(f"视频下载时 yt-dlp 未找到 [{video_id}]")
            with _download_lock:
                _download_tasks[task_key] = {
                    "status": "error",
                    "error": hint,
                    "watermark_status": watermark_status,
                }
        except Exception as e:
            with _download_lock:
                _download_tasks[task_key] = {
                    "status": "error",
                    "error": str(e),
                    "watermark_status": watermark_status,
                }

    threading.Thread(target=_do_download, daemon=True).start()
    return jsonify({
        "success": True,
        "message": f"开始下载{'（去水印模式）' if remove_watermark else ''}",
        "video_id": video_id,
        "watermark_removed": remove_watermark,
        "output_dir": output_dir,
    })


@app.route("/api/scraper/video/status")
def api_scraper_video_status():
    """查询下载任务状态"""
    video_id = request.args.get("video_id", "")
    save_dir = request.args.get("save_dir", "")
    # 兼容新旧 task_key 格式
    if save_dir:
        task_key = f"{video_id}::{save_dir}"
    else:
        task_key = video_id
    with _download_lock:
        task = _download_tasks.get(task_key)
    if not task:
        # 回退：尝试查找以 video_id 为前缀的旧格式 key
        with _download_lock:
            for key in _download_tasks:
                if key.startswith(f"{video_id}::"):
                    task = _download_tasks[key]
                    break
        if not task:
            task = _download_tasks.get(video_id)
    if not task:
        return jsonify({"status": "not_found"})
    return jsonify(task)


@app.route("/api/scraper/video/list")
def api_scraper_video_list():
    """列出已下载的视频文件"""
    download_dir = download_manager.get_effective_download_dir()
    downloaded = []
    if os.path.exists(download_dir):
        for f in sorted(glob.glob(os.path.join(download_dir, "*")), key=os.path.getmtime, reverse=True):
            if os.path.isfile(f):
                downloaded.append({
                    "filename": os.path.basename(f),
                    "filepath": f,
                    "size": os.path.getsize(f),
                    "mtime": os.path.getmtime(f),
                })
    return jsonify({
        "files": downloaded,
        "count": len(downloaded),
        "directory": download_dir,
    })


@app.route("/api/download/open-dir", methods=["POST"])
def api_open_download_dir():
    """在系统文件管理器中打开下载目录（一键直达）"""
    try:
        download_dir = download_manager.get_effective_download_dir()
        os.makedirs(download_dir, exist_ok=True)

        import platform
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", download_dir])
        elif system == "Windows":
            os.startfile(download_dir)
        else:
            subprocess.Popen(["xdg-open", download_dir])

        app.logger.info(f"已打开下载目录: {download_dir}")
        return jsonify({"success": True, "directory": download_dir})
    except Exception as e:
        app.logger.error(f"打开下载目录失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/scraper/watermark/health")
def api_watermark_health():
    """检查 TikTok 去水印 API 连通性"""
    try:
        health = check_api_health()
        return jsonify(health)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/scraper/hashtags")
def api_scraper_hashtags():
    """获取默认 hashtag 列表（以及最新保存的标签）"""
    latest = _load_latest_hashtags()
    return jsonify({
        "default_hashtags": tiktok_scraper.DEFAULT_HASHTAGS if HAS_TIKTOK_SCRAPER else [],
        "saved_hashtags": latest,
    })


# ── 标签历史持久化 ──
HASHTAG_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "logs", "hashtag_history.json")


def _load_hashtag_history():
    """加载标签修改历史"""
    if os.path.exists(HASHTAG_HISTORY_FILE):
        try:
            with open(HASHTAG_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            # 向后兼容：为旧条目补充 id
            modified = False
            for entry in history:
                if "id" not in entry:
                    entry["id"] = uuid.uuid4().hex[:8]
                    modified = True
            if modified:
                _save_hashtag_history(history)
            return history
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_hashtag_history(history):
    """持久化标签修改历史"""
    os.makedirs(os.path.dirname(HASHTAG_HISTORY_FILE), exist_ok=True)
    with open(HASHTAG_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _load_latest_hashtags():
    """加载最近一次保存的标签列表"""
    history = _load_hashtag_history()
    if history:
        return history[-1].get("hashtags", [])
    return []


@app.route("/api/scraper/hashtags/save", methods=["POST"])
def api_scraper_hashtags_save():
    """保存搜索标签并记录修改历史"""
    data = request.json or {}
    hashtags = data.get("hashtags", [])

    if not isinstance(hashtags, list) or len(hashtags) == 0:
        return jsonify({"success": False, "message": "标签列表不能为空"}), 400

    history = _load_hashtag_history()

    # 与最近一次相同时跳过，避免无意义重复记录
    if history and history[-1].get("hashtags") == hashtags:
        return jsonify({"success": True, "message": "标签未变化，已跳过", "skipped": True})

    entry = {
        "id": uuid.uuid4().hex[:8],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(hashtags),
        "hashtags": hashtags,
    }
    history.append(entry)

    # 保留最近 30 条
    if len(history) > 30:
        history = history[-30:]

    _save_hashtag_history(history)
    logger.info(f"搜索标签已保存（{len(hashtags)} 个），历史共 {len(history)} 条")
    return jsonify({"success": True, "message": "已保存", "entry": entry, "total_history": len(history)})


@app.route("/api/scraper/hashtags/history")
def api_scraper_hashtags_history():
    """获取标签修改历史列表"""
    history = _load_hashtag_history()
    return jsonify({"history": history, "count": len(history)})


@app.route("/api/scraper/hashtags/history", methods=["DELETE"])
def api_scraper_hashtags_history_delete():
    """删除标签修改历史（支持多选删除或清空全部）"""
    data = request.json or {}
    history = _load_hashtag_history()

    if data.get("clear_all"):
        _save_hashtag_history([])
        return jsonify({"success": True, "message": "已清空全部标签历史", "count": 0})

    ids = data.get("ids", [])
    if not isinstance(ids, list) or len(ids) == 0:
        return jsonify({"success": False, "message": "未指定要删除的条目"}), 400

    id_set = set(ids)
    new_history = [e for e in history if e.get("id") not in id_set]
    _save_hashtag_history(new_history)
    return jsonify({"success": True, "message": f"已删除 {len(history) - len(new_history)} 条记录", "count": len(new_history)})


# ── 搜索会话历史持久化 ──
SCRAPER_SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "logs", "scraper_sessions.json")


def _load_scraper_sessions():
    """加载搜索会话历史"""
    if os.path.exists(SCRAPER_SESSIONS_FILE):
        try:
            with open(SCRAPER_SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_scraper_sessions(sessions):
    """持久化搜索会话历史"""
    os.makedirs(os.path.dirname(SCRAPER_SESSIONS_FILE), exist_ok=True)
    with open(SCRAPER_SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)


def _save_scraper_session(hashtags, total, qualified, elapsed, task_id, engine_name):
    """保存一次搜索会话记录"""
    sessions = _load_scraper_sessions()
    session = {
        "id": task_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hashtags": hashtags,
        "hashtag_count": len(hashtags),
        "total_results": total,
        "qualified_results": qualified,
        "elapsed_sec": elapsed,
        "engine": engine_name,
    }
    sessions.append(session)
    # 保留最近 50 条
    if len(sessions) > 50:
        sessions = sessions[-50:]
    _save_scraper_sessions(sessions)
    logger.info(f"搜索会话已保存: {task_id} (total={total}, qualified={qualified})")


@app.route("/api/scraper/sessions")
def api_scraper_sessions():
    """获取搜索会话历史列表"""
    sessions = _load_scraper_sessions()
    # 倒序返回（最新的在前）
    return jsonify({"sessions": list(reversed(sessions)), "count": len(sessions)})


@app.route("/api/scraper/sessions", methods=["DELETE"])
def api_scraper_sessions_delete():
    """删除搜索会话
    请求体: {"ids": ["task_id1", "task_id2"]}  删除指定会话
            {"clear_all": true}                 清空全部
    """
    data = request.json or {}
    if data.get("clear_all"):
        _save_scraper_sessions([])
        return jsonify({"success": True, "message": "全部搜索历史已清空"})

    ids_to_delete = set(data.get("ids", []))
    if not ids_to_delete:
        return jsonify({"success": False, "message": "请指定要删除的会话 ID"}), 400

    sessions = _load_scraper_sessions()
    before = len(sessions)
    sessions = [s for s in sessions if s.get("id") not in ids_to_delete]
    deleted = before - len(sessions)
    _save_scraper_sessions(sessions)
    return jsonify({"success": True, "deleted": deleted, "message": f"已删除 {deleted} 条记录"})


# ---- 日志 ----

@app.route("/api/logs")
def api_logs():
    """获取最近的日志"""
    log_dir = LOG_DIR
    if not log_dir.exists():
        return jsonify({"logs": [], "log_files": []})

    today_log = log_dir / f"distribute_{date.today().isoformat()}.log"

    lines = []
    if today_log.exists():
        with open(today_log, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
            # 只取最后 200 行
            for line in raw_lines[-200:]:
                line = line.strip()
                if line:
                    lines.append(line)

    # 列出所有日志文件
    log_files = sorted(log_dir.glob("distribute_*.log"), reverse=True)

    return jsonify({
        "logs": lines,
        "count": len(lines),
        "today_log_exists": today_log.exists(),
        "log_files": [f.name for f in log_files]
    })


# ============================================================
# 分发任务执行器（后台线程）
# ============================================================

def run_distribution_task(dry_run=False):
    """在后台线程中执行完整的分发流程"""

    logger = logging.getLogger("TKDistributor.WebTask")
    logger.setLevel(logging.DEBUG)

    # ★ 确保日志写入文件（供 /api/logs 读取）
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"distribute_{date.today().isoformat()}.log"
    file_handler = logging.FileHandler(str(log_file), encoding="utf-8", mode="a")
    file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s",
                                                datefmt="%H:%M:%S"))
    # 避免重复添加 handler
    if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(log_file)
               for h in logger.handlers):
        logger.addHandler(file_handler)

    # 设置状态
    with state._lock:
        state.is_running = True
        state.should_stop = False

    _task_error = None  # 提前初始化，确保 finally 中可安全引用
    try:
        # ⚠️ 关键：每次启动任务都从文件重新读取最新配置
        # 避免用户修改设置后因内存缓存导致配置不生效
        config = load_config()
        if dry_run:
            config["dry_run"] = True

        allocations = {}
        success_map = {}
        devices = []
        total_videos = 0
        file_cleaner = None

        # 同步更新 state 缓存（保持一致性）
        state._lock.acquire()
        state.config = config
        state._lock.release()

        device_manager = DeviceManager(logger)
        video_allocator = VideoAllocator(config, logger)
        file_cleaner = FileCleaner(config, logger)

        # Step 1: 设备检测
        emit_progress("task_start", {"message": "开始扫描设备...", "phase": "detecting"})
        devices = device_manager.detect_devices()

        if not devices:
            emit_progress("task_error", {"message": "未检测到任何设备！请连接 iPhone 并确保信任此电脑。"})
            return

        max_batch = config.get("max_devices_per_batch", 20)
        if len(devices) > max_batch:
            devices = devices[:max_batch]

        emit_progress("devices_detected", {
            "message": f"检测到 {len(devices)} 台设备",
            "devices": [{"name": d["name"], "udid": d["udid"][:8]} for d in devices],
            "device_count": len(devices)
        })

        # Step 1.5: 【上传前清空设备旧视频】
        cleared_counts = {}  # udid → 已清理视频数
        if config.get("clear_before_upload", False):
            logger.info(f"\n{'='*50}")
            logger.info(f"上传前清空设备旧视频（共 {len(devices)} 台）")
            logger.info(f"{'='*50}")
            emit_progress("clearing_device", {
                "message": f"正在清空 {len(devices)} 台设备的旧视频...",
                "phase": "clearing"
            })

            for device in devices:
                udid = device["udid"]
                name = device["name"]
                try:
                    result = clear_iphone_photos.clear_device_videos_sync(
                        udid, media_types={"mp4", "mov", "m4v"}, dry_run=config.get("dry_run", False)
                    )
                    deleted_n = result.get("deleted", 0)
                    cleared_counts[udid] = deleted_n
                    logger.info(f"  {name}: 删除 {deleted_n} 个文件, 失败 {result.get('failed', 0)}")
                    emit_progress("device_cleared", {
                        "device_name": name,
                        "device_udid": udid[:8],
                        "deleted": deleted_n,
                        "failed": result.get("failed", 0)
                    })
                except Exception as e:
                    logger.warning(f"  清空 {name} 视频失败: {e}")
                    cleared_counts[udid] = 0
                    emit_progress("device_cleared", {
                        "device_name": name,
                        "device_udid": udid[:8],
                        "deleted": 0,
                        "failed": 1,
                        "error": str(e)
                    })

            emit_progress("clearing_done", {
                "message": f"设备旧视频清空完成"
            })

        # Step 2: 视频分配
        emit_progress("allocating", {"message": "正在分配视频..."})
        allocations = video_allocator.allocate(devices)

        if not allocations:
            emit_progress("task_error", {"message": "没有可分配的视频！请检查视频目录。"})
            return

        total_videos = sum(len(v) for v in allocations.values())
        emit_progress("allocated", {
            "message": f"分配完成：{len(allocations)} 台设备, 共 {total_videos} 个视频",
            "device_count": len(allocations),
            "video_count": total_videos
        })

        # Step 3: 并行传输到所有设备
        emit_progress("uploading", {"message": "并行传输到所有设备...", "current": 0, "total": len(devices)})
        success_map = {}
        success_devices = []
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

        # 准备上传任务
        upload_tasks = []
        for device in devices:
            udid = device["udid"]
            if udid in allocations:
                upload_tasks.append((device, allocations[udid]))
                emit_progress("device_start", {
                    "current": len(upload_tasks),
                    "total": len(devices),
                    "device_name": device["name"],
                    "device_udid": udid[:8],
                    "videos": [v.name for v in allocations[udid]]
                })

        def _upload_one_device(device, videos, dry_run):
            """在子线程中上传到一台设备（不使用asyncio，用CLI方式）"""
            udid = device["udid"]
            name = device["name"]
            device_ok = True
            if not dry_run and videos:
                results = import_to_iphone.upload_videos_batch(udid, videos)
                for vp, r in zip(videos, results):
                    if not r:
                        logger.error(f"  ❌ 上传失败: {vp.name}")
                        device_ok = False
                    else:
                        logger.info(f"  ✅ {vp.name} → {r}")
            elif dry_run:
                for vp in videos:
                    logger.info(f"  [DRY-RUN] 模拟上传: {vp.name}")
            return device, device_ok

        # 并行上传（每台设备一个线程）
        completed_count = 0
        with ThreadPoolExecutor(max_workers=len(upload_tasks)) as executor:
            futures = {executor.submit(_upload_one_device, dev, vids, dry_run): (dev, vids)
                       for dev, vids in upload_tasks}
            for future in _as_completed(futures):
                if state.should_stop:
                    emit_progress("task_stopped", {"message": "用户手动停止了任务"})
                    break
                device, device_ok = future.result()
                udid = device["udid"]
                videos = allocations.get(udid, [])

                success_map[udid] = device_ok
                if device_ok:
                    success_devices.append(device)

                completed_count += 1
                status_icon = "✅" if device_ok else "❌"
                status_text = "成功" if device_ok else "失败"
                video_names = [v.name for v in videos]
                cleared_n = cleared_counts.get(udid, 0)

                full_msg_parts = [f"[{completed_count}/{len(devices)}] {device['name']}: {status_icon} {status_text}"]
                if video_names:
                    full_msg_parts.append(f"  上传 {'  '.join(video_names)}")
                if cleared_n > 0:
                    full_msg_parts.append(f"    清理旧视频 {cleared_n} 条")

                emit_progress("device_done", {
                    "current": completed_count,
                    "total": len(devices),
                    "device_name": device["name"],
                    "device_udid": udid[:8],
                    "success": device_ok,
                    "message": "".join(full_msg_parts),
                    "videos": video_names,
                    "cleared_count": cleared_n
                })

        # Step 4: 重建索引 + 重启（发送命令后直接完成，不等 UDID 匹配）
        if not state.should_stop and success_devices:
            logger.info(f"\n{'='*50}")
            logger.info(f"全部上传完成，发送重启命令")
            logger.info(f"{'='*50}")

            restart_ok = []

            # 并行 CLI 重启（所有设备同时发命令）
            import subprocess as _sp, threading as _thd, time as _tm
            _results = {}
            _lock = _thd.Lock()

            def _reboot_one(device):
                udid = device["udid"]
                name = device["name"]
                for _try in range(2):
                    try:
                        # Step 1: rename Photos.sqlite files with afcclient
                        _ts = str(int(_tm.time()))
                        renamed = 0
                        for _f in ['Photos.sqlite', 'Photos.sqlite-wal', 'Photos.sqlite-shm']:
                            try:
                                _r = _sp.run(
                                    ['afcclient', '-u', udid],
                                    input=f"mv /PhotoData/{_f} /PhotoData/{_f}.bak_{_ts}\nexit\n",
                                    capture_output=True, text=True, timeout=10
                                )
                                if _r.returncode == 0:
                                    renamed += 1
                            except Exception:
                                pass
                        logger.info(f"  📝 {name}: renamed {renamed}/3 db files")

                        # Step 2: trigger restart with idevicediagnostics
                        _r2 = _sp.run(
                            ['idevicediagnostics', '-u', udid, 'restart'],
                            capture_output=True, text=True, timeout=15
                        )
                        if _r2.returncode == 0:
                            logger.info(f"  🔄 {name}: restart sent")
                            with _lock:
                                _results[udid] = (True, name)
                            return
                        else:
                            logger.warning(f"  ⚠️ {name}: idevicediagnostics rc={_r2.returncode}")
                    except Exception as e:
                        logger.error(f"  ⚠️ {name}: retry {_try+1} failed: {type(e).__name__}: {e}")
                    _tm.sleep(2)
                logger.error(f"  ❌ {name}: all retries failed")
                with _lock:
                    _results[udid] = (False, name)

            # 并行发送重启命令到所有设备（一次性全部执行）
            device_names = [d["name"] for d in success_devices]
            emit_progress("restart_start", {
                "message": f"发送重启命令到 {len(success_devices)} 台设备: {', '.join(device_names)}",
                "device_count": len(success_devices),
                "batch_count": 1
            })

            logger.info(f"  → 并行发送重启命令 ({len(success_devices)}台)...")
            _bt = [_thd.Thread(target=_reboot_one, args=(d,), daemon=True) for d in success_devices]
            for t in _bt:
                t.start()
            for t in _bt:
                t.join(timeout=60)

            restart_ok = [d for d in success_devices if _results.get(d["udid"], (False,))[0]]
            for udid, (ok, name) in _results.items():
                logger.info(f"  {'[OK]' if ok else '[FAIL]'} {name}")

            # 短暂等待确认设备开始重启，然后直接完成（不等 UDID 验证）
            if restart_ok:
                logger.info(f"⏳ 等待 {len(restart_ok)} 台设备断开确认重启...")
                emit_progress("restart_waiting", {
                    "message": f"等待 {len(restart_ok)} 台设备断开...",
                    "wait_seconds": 30,
                    "device_count": len(restart_ok),
                    "online_count": len(restart_ok)
                })
                # 等待设备离线确认（最多 30 秒），不验证上线
                _start = _tm.time()
                _udid_set = set(d["udid"] for d in restart_ok)
                _disconnected = False
                while _tm.time() - _start < 30:
                    try:
                        _online = _sp.run(
                            ["idevice_id", "-l"],
                            capture_output=True, text=True, timeout=10
                        )
                        _on_set = set(_online.stdout.strip().split("\n")) if _online.stdout.strip() else set()
                        _still = _udid_set & _on_set
                        if len(_still) < len(_udid_set):
                            _disconnected = True
                            logger.info(f"🔌 设备已断开（{len(_still)}/{len(_udid_set)} 仍在线），确认重启中")
                            break
                    except Exception:
                        pass
                    emit_progress("restart_waiting", {
                        "message": f"等待设备响应重启... {len(_udid_set)}台",
                        "wait_seconds": int(30 - (_tm.time() - _start)),
                        "device_count": len(_udid_set),
                        "online_count": len(_udid_set)
                    })
                    _tm.sleep(3)

                if not _disconnected:
                    logger.info(f"未检测到断开，设备可能已快速重启")

            emit_progress("restart_done", {
                "message": f"重启完成: {len(restart_ok)}/{len(success_devices)} 台设备",
                "device_count": len(restart_ok)
            })

        # Step 5: 标记历史
        if not state.should_stop:
            video_allocator.mark_as_distributed(allocations, success_map)

        # Step 6: 清理（实际清理在 finally 中执行，确保异常也不跳过）
        emit_progress("cleaning", {"message": "清理本地文件..."})

    except Exception as e:
        logger.error(f"分发任务异常: {e}", exc_info=True)
        _task_error = str(e)

    finally:
        # 发送完成事件（无论异常与否）
        try:
            success_count = sum(1 for s in success_map.values() if s) if success_map else 0
            fail_count = len(success_map) - success_count if success_map else 0
            emit_progress("task_complete" if not _task_error else "task_error", {
                "message": f"分发完成！{success_count}成功/{fail_count}失败" if not _task_error else f"任务异常: {_task_error}",
                "summary": {
                    "devices_total": len(devices),
                    "success_count": success_count,
                    "fail_count": fail_count,
                    "video_count": total_videos
                }
            })
        except Exception:
            pass

        # ★ 始终执行清理（即使异常也删已分发的文件）
        try:
            if file_cleaner and allocations and success_map and config.get("auto_delete_after_upload", False):
                file_cleaner.clean_uploaded_files(allocations, success_map, dry_run=dry_run)
        except Exception as _ce:
            logger.warning(f"清理异常: {_ce}")

        with state._lock:
            state.is_running = False
            state.should_stop = False


# ============================================================
# 启动入口
# ============================================================

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  🎬 TK 视频分发工具 - Web 管理界面")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5800))
    print(f"\n  访问地址: http://localhost:{port}")
    print("  按 Ctrl+C 停止服务\n")

    # 确保 templates 目录存在
    (SCRIPT_DIR / "templates").mkdir(exist_ok=True)

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
