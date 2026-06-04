#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TK 视频分发工具 - 桌面版
========================
PyQt6 桌面应用，通过 pyautogui 操控爱思助手 GUI 实现视频导入 iPhone 相册

用法:
    python3 tk_distributor_app.py

工作流:
    1. 连接 N 台 iPhone → 打开应用 → 刷新设备
    2. 点"开始分发" → 自动操控爱思助手逐台导入
    3. 完成 → 弹窗"是否删除已导入视频?" → 是/否
    4. 换下一批手机 → 回到第1步
"""

import sys
import os
import time
import logging
import threading
from pathlib import Path
from datetime import datetime

# ============================================================
# 确保项目根目录在路径中（用于 import 子模块）
# ============================================================
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# PyQt6 导入
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QTextEdit, QProgressBar,
    QLineEdit, QFileDialog, QMessageBox, QFrame, QSplitter,
    QGroupBox, QSizePolicy, QSystemTrayIcon, QMenu,
    QCheckBox, QComboBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QFont, QColor, QIcon, QTextCharFormat, QPalette, QTextCursor

# 项目模块
from tk_distributor_core import (
    load_config, CONFIG_FILE, LOG_DIR,
    DeviceManager, VideoAllocator, FileCleaner,
)


# ============================================================
# 日志配置
# ============================================================

def setup_logging():
    """配置日志同时输出到文件和控制台"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = LOG_DIR / f"distribute_{datetime.now().strftime('%Y%m%d')}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 文件 handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))

    # 控制台 handler (只显示 INFO 以上)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))

    root_logger.addHandler(fh)
    root_logger.addHandler(ch)

    return logging.getLogger("TKDistributor.App")


app_logger = setup_logging()


# ============================================================
# 后台任务线程
# ============================================================

class DistributeWorker(QThread):
    """后台分发任务线程"""

    # 信号定义
    log_signal = pyqtSignal(str)                    # 日志消息 (支持颜色标记: [INFO]/[OK]/[WARN]/[ERR])
    progress_signal = pyqtSignal(int, int)           # 当前进度, 总数
    device_start_signal = pyqtSignal(dict)          # 开始处理设备 {current, total, name, udid, videos}
    device_done_signal = pyqtSignal(dict)           # 设备完成 {current, total, name, success, videos_count}
    finished_signal = pyqtSignal(dict)              # 全部完成 {total, success, failed, video_count, allocations, success_map}
    error_signal = pyqtSignal(str)                 # 致命错误
    warning_signal = pyqtSignal(str)               # 警告消息

    def __init__(self, config_override=None):
        super().__init__()
        self.should_stop = False
        self.config_override = config_override or {}

    def stop(self):
        """请求停止任务"""
        self.should_stop = True

    def run(self):
        """执行完整分发流程"""
        try:
            self._execute()
        except Exception as e:
            app_logger.exception("分发任务异常")
            self.error_signal.emit(f"任务异常: {str(e)}")

    def _emit_log(self, message: str, level: str = "INFO"):
        """
        发送日志信号。

        level: INFO | OK | WARN | ERR
             前端根据 level 用不同颜色渲染。
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_signal.emit(f"[{timestamp}] [{level}] {message}")
        # 同时写到项目日志
        if level == "ERR":
            app_logger.error(message)
        elif level == "WARN":
            app_logger.warning(message)
        elif level == "OK":
            app_logger.info(f"✓ {message}")
        else:
            app_logger.info(message)

    def _execute(self):
        # ====== Phase 1: 加载配置 + 设备检测 ======
        self._emit_log("正在加载配置...")
        config = load_config()

        # 合并界面传入的覆盖配置（如视频目录变更）
        if self.config_override:
            config.update(self.config_override)

        self._emit_log(f"视频目录: {config.get('video_source_dir_abs', 'N/A')}")

        self._emit_log("正在扫描设备...")
        device_manager = DeviceManager(app_logger)
        devices = device_manager.detect_devices()

        if not devices:
            self.warning_signal.emit("未检测到任何设备！请确认:")
            self.warning_signal.emit("  1. iPhone 已通过 USB 连接到 Mac")
            self.warning_signal.emit("  2. iPhone 上已信任此电脑")
            self.finished_signal.emit({
                "total": 0, "success": 0, "failed": 0,
                "video_count": 0, "allocations": {}, "success_map": {},
            })
            return

        max_batch = config.get("max_devices_per_batch", 20)
        if len(devices) > max_batch:
            devices = devices[:max_batch]
            self._emit_log(f"设备数量超限，只处理前 {max_batch} 台", "WARN")

        self._emit_log(f"检测到 {len(devices)} 台设备:", "OK")
        for d in devices:
            self._emit_log(f"  📱 {d['name']} ({d['udid'][:8]}...)")

        # ====== Phase 2: 视频分配 ======
        self._emit_log("正在分配视频...")
        video_allocator = VideoAllocator(config, app_logger)
        allocations = video_allocator.allocate(devices)

        if not allocations:
            self.error_signal.emit("没有可分配的视频！请检查视频目录是否有可用文件。")
            return

        total_videos = sum(len(v) for v in allocations.values())
        self._emit_log(
            f"分配完成: {len(allocations)} 台设备 × 每台 "
            f"{config.get('videos_per_device', 3)} 个 = 共 {total_videos} 个视频",
            "OK"
        )

        # ====== Phase 2.5: 清空手机旧视频（可选）======
        if config.get("clear_before_upload", False):
            from tk_distributor_core import PhotoCleaner
            photo_cleaner = PhotoCleaner(config, app_logger)

            self._emit_log("🗑️ 正在清空各设备相机胶卷旧视频...")
            for device in devices:
                if self.should_stop:
                    break
                photo_cleaner.clear_old_videos(
                    device["udid"],
                    device.get("name", "")
                )
            self._emit_log("✅ 旧视频清理完成", "OK")

        # ====== Phase 3: 延迟导入 i4tools_remote 避免循环导入问题 ======
        # 在这里才动态导入 ImportController，因为它是独立模块
        self._emit_log("初始化爱思助手遥控器...")

        # 动态导入避免与 PyQt6 的冲突（pyautogui 内部可能有一些兼容性问题）
        try:
            from i4tools_remote import ImportController
        except ImportError as e:
            self.error_signal.emit(f"无法导入爱思遥控器模块: {e}\n请确认 i4tools_remote.py 存在且依赖已安装。")
            return

        controller = ImportController()
        success_map = {}
        total_operations = len(devices) * config.get("videos_per_device", 3)
        op_count = 0

        # ====== Phase 4: 逐台逐视频导入 ======
        for i, device in enumerate(devices):
            if self.should_stop:
                self._emit_log("用户停止了分发任务", "WARN")
                break

            udid = device["udid"]
            videos = allocations.get(udid, [])

            if not videos:
                self._emit_log(f"[{i+1}/{len(devices)}] {device['name']}: 无可分配视频", "WARN")
                continue

            # 发出设备开始信号
            self.device_start_signal.emit({
                "current": i + 1,
                "total": len(devices),
                "name": device["name"],
                "udid": udid,
                "videos": [v.name for v in videos],
            })

            device_all_ok = True

            for j, video_path in enumerate(videos):
                if self.should_stop:
                    break

                op_count += 1
                video_name = video_path.name
                file_size_mb = video_path.stat().st_size / 1024 / 1024

                self._emit_log(
                    f"📍 [{i+1}/{len(devices)}] {device['name']}: "
                    f"导入 {video_name} ({file_size_mb:.1f} MB) [{j+1}/{len(videos)}]"
                )

                # 更新总进度
                self.progress_signal.emit(op_count, total_operations)

                # 调用爱思遥控器执行实际导入
                start_t = time.time()
                result = controller.import_video(str(video_path), device_name=device["name"])
                elapsed = time.time() - start_t
                ok = result.get("success", False)

                if ok:
                    self._emit_log(
                        f"  ✓ 导入成功: {video_name} ({elapsed:.0f}s)",
                        "OK"
                    )
                else:
                    self._emit_log(
                        f"  ✗ 导入失败: {video_name}",
                        "ERR"
                    )
                    device_all_ok = False

                # 设备内视频之间短暂间隔
                if j < len(videos) - 1 and not self.should_stop:
                    time.sleep(1.5)

            success_map[udid] = device_all_ok

            status_text = "✅ 成功" if device_all_ok else "✗ 失败"
            self.device_done_signal.emit({
                "current": i + 1,
                "total": len(devices),
                "name": device["name"],
                "udid": udid,
                "success": device_all_ok,
                "videos_count": len(videos),
            })

            # 设备之间间隔（给爱思助手缓冲时间）
            if i < len(devices) - 1 and not self.should_stop:
                time.sleep(2)

        # ====== Phase 4.5: 重建照片库 + 重启（可选）======
        if config.get("clear_before_upload", False) and success_map:
            from tk_distributor_core import PhotoCleaner
            photo_cleaner = PhotoCleaner(config, app_logger)

            processed_devices = [
                {"udid": d["udid"], "name": d["name"]}
                for d in devices
            ]
            self._emit_log(
                f"📸 正在重建 {len(processed_devices)} 台设备的照片库...",
                "INFO"
            )
            photo_cleaner.rebuild_all(processed_devices)
            self._emit_log(
                "💡 等待 60-90 秒后打开 iPhone「照片」App 即可看到新视频",
                "INFO"
            )

        # ====== Phase 5: 完成 ======
        success_count = sum(1 for v in success_map.values() if v)
        fail_count = len(success_map) - success_count

        self._emit_log("")
        self._emit_log("=" * 50)
        self._emit_log(f"🎉 分发完成!", "OK")
        self._emit_log(f"  设备: 成功 {success_count} / 失败 {fail_count} / 共 {len(devices)}")
        self._emit_log(f"  视频: {total_videos} 个")
        self._emit_log("=" * 50)

        self.progress_signal.emit(total_operations, total_operations)

        self.finished_signal.emit({
            "total": len(devices),
            "success": success_count,
            "failed": fail_count,
            "video_count": total_videos,
            "allocations": allocations,
            "success_map": success_map,
        })


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    """TK 视频分发工具 - 主窗口"""

    def __init__(self):
        super().__init__()
        self.worker = None
        self.running = False
        self.current_config = load_config()

        self.setWindowTitle("🎬 TK 视频分发工具 v1.1.2")
        self.setMinimumSize(920, 720)
        self.resize(1000, 780)

        self._setup_ui()
        self._load_initial_state()
        self._apply_dark_style()

        # 定时刷新设备数（每30秒自动检测）
        self.auto_refresh_timer = QTimer()
        self.auto_refresh_timer.timeout.connect(self.on_refresh_devices)
        # 不自动启动，用户手动刷新更可控

    # ---- UI 构建 ----

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # ====== 顶部：视频目录选择 ======
        dir_layout = QHBoxLayout()
        dir_label = QLabel("📁 视频目录:")
        dir_label.setStyleSheet("font-size: 13px; color: #94a3b8;")
        self.dir_input = QLineEdit()
        self.dir_input.setReadOnly(True)
        self.dir_input.setStyleSheet("""
            QLineEdit {
                background: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                color: #e2e8f0;
                font-size: 13px;
            }
        """)
        self.dir_btn = QPushButton("📂 选择文件夹")
        self.dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dir_btn.clicked.connect(self.on_choose_directory)
        self._style_primary_button(self.dir_btn)

        dir_layout.addWidget(dir_label)
        dir_layout.addWidget(self.dir_input, 1)
        dir_layout.addWidget(self.dir_btn)
        layout.addLayout(dir_layout)

        # ====== ⚙️ 分发设置面板 ======
        settings_group = QGroupBox("⚙️ 分发设置")
        settings_group.setStyleSheet(self._group_style())
        settings_layout = QVBoxLayout(settings_group)

        # 第一行：两个复选框
        row1 = QHBoxLayout()

        self.auto_del_checkbox = QCheckBox("分发后自动删除本地视频")
        self.auto_del_checkbox.setChecked(
            self.current_config.get("auto_delete_after_upload", True)
        )
        self.auto_del_checkbox.toggled.connect(self._on_setting_changed)
        self.auto_del_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)

        self.clear_checkbox = QCheckBox("上传前清空手机旧视频（含照片库重建+重启）")
        self.clear_checkbox.setChecked(
            self.current_config.get("clear_before_upload", False)
        )
        self.clear_checkbox.toggled.connect(self._on_setting_changed)
        self.clear_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_checkbox.setToolTip(
            "开启后，每次分发前先删除设备 DCIM 中的旧视频，\n"
            "上传新视频后自动重建照片库并重启所有 iPhone。"
        )

        row1.addWidget(self.auto_del_checkbox)
        row1.addSpacing(24)
        row1.addWidget(self.clear_checkbox)
        row1.addStretch()

        # 第二行：数字设置
        row2 = QHBoxLayout()

        per_device_label = QLabel("每台分配")
        per_device_label.setStyleSheet("color: #94a3b8; font-size: 13px;")
        self.per_device_combo = QComboBox()
        self.per_device_combo.addItems(["2", "3", "4", "5"])
        self.per_device_combo.setCurrentText(
            str(self.current_config.get("videos_per_device", 3))
        )
        self.per_device_combo.currentTextChanged.connect(self._on_setting_changed)
        self.per_device_combo.setFixedWidth(60)
        per_device_suffix = QLabel("个视频")
        per_device_suffix.setStyleSheet("color: #94a3b8; font-size: 13px;")

        batch_label = QLabel("最大批次")
        batch_label.setStyleSheet("color: #94a3b8; font-size: 13px;")
        self.batch_combo = QComboBox()
        self.batch_combo.addItems(["2", "3", "4", "5", "10", "20"])
        self.batch_combo.setCurrentText(
            str(self.current_config.get("max_devices_per_batch", 5))
        )
        self.batch_combo.currentTextChanged.connect(self._on_setting_changed)
        self.batch_combo.setFixedWidth(60)
        batch_suffix = QLabel("台设备")
        batch_suffix.setStyleSheet("color: #94a3b8; font-size: 13px;")

        row2.addWidget(per_device_label)
        row2.addWidget(self.per_device_combo)
        row2.addWidget(per_device_suffix)
        row2.addSpacing(24)
        row2.addWidget(batch_label)
        row2.addWidget(self.batch_combo)
        row2.addWidget(batch_suffix)
        row2.addStretch()

        settings_layout.addLayout(row1)
        settings_layout.addLayout(row2)
        layout.addWidget(settings_group)

        # ====== 中部：左右分栏 ======
        mid_widget = QWidget()
        mid_layout = QHBoxLayout(mid_widget)
        mid_layout.setContentsMargins(0, 0, 0, 0)
        mid_layout.setSpacing(10)

        # --- 左侧：设备列表 ---
        left_group = QGroupBox("📱 已连接设备")
        left_group.setStyleSheet(self._group_style())
        left_layout = QVBoxLayout(left_group)

        # 设备列表
        self.device_list = QListWidget()
        self.device_list.setStyleSheet("""
            QListWidget {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 4px;
                font-size: 13px;
                color: #cbd5e1;
            }
            QListWidget::item {
                padding: 6px 8px;
                border-radius: 4px;
                margin: 2px 0;
            }
            QListWidget::item:selected {
                background: #1d4ed8;
                color: white;
            }
            QListWidget::item:hover {
                background: #1e293b;
            }
        """)
        self.device_list.setMinimumWidth(260)

        # 刷新按钮
        refresh_btn = QPushButton("🔄 刷新设备")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self.on_refresh_devices)
        self._style_secondary_button(refresh_btn)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(refresh_btn)
        btn_layout.addStretch()

        left_layout.addWidget(self.device_list, 1)
        left_layout.addLayout(btn_layout)
        mid_layout.addWidget(left_group, 1)

        # --- 右侧：视频信息 ---
        right_group = QGroupBox("📦 待分发视频")
        right_group.setStyleSheet(self._group_style())
        right_layout = QVBoxLayout(right_group)

        self.video_info_labels = {}
        info_items = [
            ("available", "可用视频:", "--"),
            ("per_device", "每台分配:", f"{self.current_config.get('videos_per_device', 3)} 个"),
            ("total_needed", "本批需求:", "--"),
            ("dir_exists", "目录状态:", "检测中..."),
        ]
        for key, label_text, default in info_items:
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #94a3b8; font-size: 13px;")
            val = QLabel(default)
            val.setStyleSheet("color: #e2e8f0; font-size: 13px; font-weight: bold;")
            val.setObjectName(key)
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(val)
            right_layout.addLayout(row)
            self.video_info_labels[key] = val

        # 配置预览
        config_preview = QLabel()
        config_preview.setText(
            f"最大批次: {self.current_config.get('max_devices_per_batch', 20)} 台 | "
            f"目标路径: {self.current_config.get('phone_target_path', 'DCIM/100APPLE')}"
        )
        config_preview.setStyleSheet("color: #64748b; font-size: 11px; margin-top: 8px;")
        right_layout.addWidget(config_preview)
        right_layout.addStretch()

        mid_layout.addWidget(right_group, 1)

        layout.addWidget(mid_widget, 3)

        # ====== 操作按钮区 ======
        action_layout = QHBoxLayout()
        action_layout.setSpacing(12)

        self.start_btn = QPushButton("▶️  开始分发")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.setMinimumHeight(44)
        self.start_btn.clicked.connect(self.on_start_distribute)
        self._style_action_button(self.start_btn, is_start=True)

        self.stop_btn = QPushButton("⏹️  停止")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setMinimumHeight(44)
        self.stop_btn.clicked.connect(self.on_stop_distribute)
        self.stop_btn.setEnabled(False)
        self._style_action_button(self.stop_btn, is_start=False)

        action_layout.addStretch()
        action_layout.addWidget(self.start_btn)
        action_layout.addWidget(self.stop_btn)
        action_layout.addStretch()
        layout.addLayout(action_layout)

        # ====== 进度条区域 ====== progress_group
        progress_group = QGroupBox("⏳ 进度")
        progress_group.setStyleSheet(self._group_style())
        progress_layout = QVBoxLayout(progress_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("准备就绪")
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background: #1e293b;
                border: 1px solid #334155;
                border-radius: 8px;
                height: 24px;
                text-align: center;
                color: #94a3b8;
                font-size: 12px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3b82f6, stop:1 #60a5fa);
                border-radius: 7px;
            }
        """)

        self.status_label = QLabel("就绪 — 请连接 iPhone 并点击「刷新设备」")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px; padding: 2px 0;")

        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.status_label)
        layout.addWidget(progress_group)

        # ====== 警告横幅（运行时显示）=====
        self.warning_banner = QLabel("⚠️  正在通过爱思助手自动导入，请勿操作鼠标键盘！")
        self.warning_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.warning_banner.setStyleSheet("""
            QLabel {
                background: linear-gradient(90deg, #92400e, #b45309);
                color: #fef3c7;
                padding: 10px;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }
        """)
        self.warning_banner.hide()
        layout.addWidget(self.warning_banner)

        # ====== 底部：日志区域 ======
        log_group = QGroupBox("📋 运行日志")
        log_group.setStyleSheet(self._group_style())
        log_layout = QVBoxLayout(log_group)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px;
                font-family: 'Menlo', 'Monaco', 'Consolas', monospace;
                font-size: 12px;
                color: #94a3b8;
                line-height: 1.4;
            }
        """)
        # 设置最小高度保证日志区足够大
        self.log_text.setMinimumHeight(200)

        # 日志底部操作
        log_bottom = QHBoxLayout()
        clear_log_btn = QPushButton("🗑️ 清空日志")
        clear_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_log_btn.clicked.connect(lambda: self.log_text.clear())
        self._style_secondary_button(clear_log_btn)

        open_log_dir_btn = QPushButton("📂 打开日志目录")
        open_log_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_log_dir_btn.clicked.connect(self.on_open_log_dir)
        self._style_secondary_button(open_log_dir_btn)

        log_bottom.addStretch()
        log_bottom.addWidget(clear_log_btn)
        log_bottom.addWidget(open_log_dir_btn)
        log_layout.addWidget(self.log_text, 1)
        log_layout.addLayout(log_bottom)

        layout.addWidget(log_group, 4)

    # ---- 样式辅助方法 ----

    @staticmethod
    def _group_style() -> str:
        return """
            QGroupBox {
                font-size: 13px;
                font-weight: bold;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
        """

    @staticmethod
    def _style_primary_button(btn: QPushButton):
        btn.setStyleSheet("""
            QPushButton {
                background: #2563eb;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover { background: #1d4ed8; }
            QPushButton:pressed { background: #1e40af; }
            QPushButton:disabled { background: #374151; color: #6b7280; }
        """)

    @staticmethod
    def _style_secondary_button(btn: QPushButton):
        btn.setStyleSheet("""
            QPushButton {
                background: #334155;
                color: #cbd5e1;
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton:hover { background: #475569; color: white; }
            QPushButton:pressed { background: #1e293b; }
            QPushButton:disabled { background: #1e293b; color: #475569; }
        """)

    @staticmethod
    def _style_action_button(btn: QPushButton, is_start: bool):
        if is_start:
            btn.setStyleSheet("""
                QPushButton {
                    background: #059669;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 10px 28px;
                    font-size: 15px;
                    font-weight: bold;
                }
                QPushButton:hover { background: #047857; }
                QPushButton:pressed { background: #065f46; }
                QPushButton:disabled { background: #374151; color: #6b7280; }
            """)
        else:
            btn.setStyleSheet("""
                QPushButton {
                    background: #dc2626;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 10px 28px;
                    font-size: 15px;
                    font-weight: bold;
                }
                QPushButton:hover { background: #b91c1c; }
                QPushButton:pressed { background: #991b1b; }
                QPushButton:disabled { background: #374151; color: #6b7280; }
            """)

    def _apply_dark_style(self):
        """应用深色主题到整个窗口"""
        self.setStyleSheet("""
            QMainWindow {
                background: #0f172a;
            }
            QWidget {
                background: #0f172a;
                color: #e2e8f0;
            }
            QLabel {
                color: #e2e8f0;
            }
        """)

    # ---- 设置变更 ----

    def _on_setting_changed(self):
        """设置变更时自动保存到 config.yaml"""
        try:
            import yaml
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

            cfg["auto_delete_after_upload"] = self.auto_del_checkbox.isChecked()
            cfg["clear_before_upload"] = self.clear_checkbox.isChecked()
            cfg["videos_per_device"] = int(self.per_device_combo.currentText())
            cfg["max_devices_per_batch"] = int(self.batch_combo.currentText())

            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

            self.current_config = cfg

            # 同步更新视频信息面板
            self.refresh_video_info()
        except Exception:
            pass  # 静默失败，不影响主流程

    # ---- 数据加载 ----

    def _load_initial_state(self):
        """启动时加载初始数据"""
        config = self.current_config

        # 显示当前视频目录
        video_dir = config.get("video_source_dir_abs",
                                config.get("video_source_dir", "./videos_ready"))
        self.dir_input.setText(str(video_dir))

        # 更新视频概况
        self.refresh_video_info()

        # 初始日志 - 版本信息
        self.append_log("🎬 TK 视频分发工具 v1.1.2 已启动", "OK")
        self.append_log("━" * 40, "INFO")
        self.append_log("📋 更新日志 (v1.1.2):", "INFO")
        self.append_log("   🆕 爆款素材：TikTok 热门视频爬虫", "INFO")
        self.append_log("   🚀 并行上传：多台设备同时传输，提速约 2.5 倍", "INFO")
        self.append_log("   🔧 整合爬虫到 Web 端统一管理", "INFO")
        self.append_log("━" * 40, "INFO")
        self.append_log(f"   视频目录: {video_dir}", "INFO")
        self.append_log(f"   每台分配: {config.get('videos_per_device', 3)} 个视频", "INFO")
        self.append_log(f"   清空旧视频: {'✅ 已开启' if config.get('clear_before_upload', False) else '⦻ 未开启'}", "INFO")
        self.append_log("", "INFO")

    def refresh_video_info(self):
        """刷新视频目录信息"""
        from tk_distributor_core import VideoAllocator
        config = self.current_config
        video_dir = Path(config.get("video_source_dir_abs",
                                    config.get("video_source_dir", "./videos_ready")))

        if video_dir.exists():
            allocator = VideoAllocator(config, app_logger)
            available = allocator.get_available_videos()
            count = len(available)
            per_device = config.get("videos_per_device", 3)
            device_count = self.device_list.count() or 1

            self.video_info_labels["available"].setText(f"{count} 个")
            self.video_info_labels["available"].setStyleSheet(
                "color: #34d399; font-size: 13px; font-weight: bold;" if count > 0
                else "color: #f87171; font-size: 13px; font-weight: bold;"
            )
            self.video_info_labels["per_device"].setText(f"{per_device} 个")
            needed = min(device_count * per_device, count)
            self.video_info_labels["total_needed"].setText(f"{needed} 个")
            self.video_info_labels["dir_exists"].setText("✅ 目录正常")
            self.video_info_labels["dir_exists"].setStyleSheet("color: #34d399; font-size: 13px; font-weight: bold;")
        else:
            self.video_info_labels["available"].setText("N/A")
            self.video_info_labels["total_needed"].setText("--")
            self.video_info_labels["dir_exists"].setText("❌ 目录不存在")
            self.video_info_labels["dir_exists"].setStyleSheet("color: #f87171; font-size: 13px; font-weight: bold;")

    # ---- 事件处理 ----

    def on_choose_directory(self):
        """选择视频源目录"""
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "选择视频源目录",
            str(self.dir_input.text()),
            QFileDialog.Option.ShowDirsOnly
        )
        if dir_path:
            self.dir_input.setText(dir_path)
            # 更新配置中的视频目录
            self.current_config["video_source_dir"] = dir_path
            self.current_config["video_source_dir_abs"] = Path(dir_path).resolve()

            # 同时写入 config.yaml 持久化
            try:
                import yaml
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
                config["video_source_dir"] = dir_path
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                self.append_log(f"视频目录已更新: {dir_path}", "OK")
            except Exception as e:
                self.append_log(f"保存目录设置失败: {e}", "ERR")

            self.refresh_video_info()

    def on_refresh_devices(self):
        """刷新设备列表"""
        self.append_log("正在扫描 iOS 设备...", "INFO")
        self.device_list.clear()

        device_manager = DeviceManager(app_logger)
        devices = device_manager.detect_devices()

        if not devices:
            self.device_list.addItem("  ⚠️ 未检测到设备 — 请连接 iPhone")
            self.append_log("未检测到任何设备", "WARN")
            self.status_label.setText("未检测到设备 — 请连接 iPhone 并刷新")
            self.refresh_video_info()
            return

        for i, d in enumerate(devices, 1):
            item_text = f"  {i}. {d['name']}  ({d['udid'][:8]}...)"
            self.device_list.addItem(item_text)

        self.append_log(f"检测到 {len(devices)} 台 iOS 设备", "OK")
        self.status_label.setText(f"已连接 {len(devices)} 台设备 — 准备就绪")
        self.progress_bar.setFormat(f"就绪 - {len(devices)} 台设备待分发")

        # 更新视频信息（需要知道设备数量才能算总需求数）
        self.refresh_video_info()

    def on_start_distribute(self):
        """开始分发"""
        # 前置检查
        if self.device_list.count() == 0 or \
           (self.device_list.count() == 1 and "未检测" in self.device_list.item(0).text()):
            QMessageBox.warning(self, "提示", "请先连接 iPhone 并点击「刷新设备」")
            return

        video_dir_str = self.dir_input.text()
        if not video_dir_str or not Path(video_dir_str).exists():
            QMessageBox.warning(self, "提示", "视频目录不存在，请先选择有效的视频目录")
            return

        # 确认对话框
        device_count = self.device_list.count()
        reply = QMessageBox.question(
            self, "确认开始分发",
            f"即将对 {device_count} 台设备进行视频分发。\n\n"
            f"⚠️ 分发过程中将自动操控爱思助手，\n"
            f"   请不要操作鼠标和键盘。\n\n"
            f"是否立即开始？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.running = True
        self.toggle_buttons(False)
        self.warning_banner.show()
        self.status_label.setText("正在分发中...")
        self.progress_bar.setValue(0)

        # 启动后台线程
        self.worker = DistributeWorker(config_override={
            "video_source_dir_abs": Path(video_dir_str).resolve(),
        })
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.device_start_signal.connect(self.on_device_start)
        self.worker.device_done_signal.connect(self.on_device_done)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.error_signal.connect(self.on_error)
        self.worker.warning_signal.connect(self.on_warning)

        self.worker.start()
        self.append_log("━━━ 分发任务已启动 ━━━", "INFO")

    def on_stop_distribute(self):
        """停止分发"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "确认停止",
                "确定要停止当前的分发任务吗？\n(当前设备的本次操作仍会完成)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.worker.stop()
                self.append_log("正在等待当前操作完成后停止...", "WARN")

    def toggle_buttons(self, enabled: bool):
        """切换按钮状态（运行时禁用部分控件）"""
        self.start_btn.setEnabled(enabled)
        self.stop_btn.setEnabled(not enabled)
        self.dir_btn.setEnabled(enabled)
        # 设备列表运行时不可修改
        # self.device_list.setEnabled(enabled)

    # ---- 信号槽 ----

    def append_log(self, message: str, level: str = "INFO"):
        """追加日志到文本框（带颜色）"""
        # 解析 level
        color_map = {
            "INFO": "#94a3b8",
            "OK": "#34d399",
            "WARN": "#fbbf24",
            "ERR": "#f87171",
        }
        color = color_map.get(level, "#94a3b8")

        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        format_ = QTextCharFormat()
        format_.setForeground(QColor(color))

        # 如果是纯时间戳行或空行，不加前缀
        text = message
        if not text.startswith("=") and not text.startswith("━") and text.strip():
            pass  # 保持原样

        cursor.insertText(text + "\n", format_)
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def update_progress(self, current: int, total: int):
        """更新进度条"""
        if total > 0:
            pct = int(current / total * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{current}/{total} ({pct}%)")

    def on_device_start(self, info: dict):
        """某设备开始处理"""
        current = info.get("current", "?")
        total = info.get("total", "?")
        name = info.get("name", "")
        videos = info.get("videos", [])
        self.status_label.setText(
            f"[{current}/{total}] {name} — 正在导入..."
        )
        self.append_log(
            f"▶ 开始处理 [{current}/{total}] {name}: {len(videos)} 个视频",
            "INFO"
        )

    def on_device_done(self, info: dict):
        """某设备处理完成"""
        current = info.get("current", "?")
        total = info.get("total", "?")
        name = info.get("name", "?")
        success = info.get("success", False)
        vc = info.get("videos_count", 0)
        status = "✅" if success else "✗"
        self.append_log(
            f"■ [{current}/{total}] {name} 完成 {status} ({vc} 个视频)",
            "OK" if success else "ERR"
        )

    def on_finished(self, result: dict):
        """全部完成"""
        self.running = False
        self.toggle_buttons(True)
        self.warning_banner.hide()

        total = result.get("total", 0)
        success = result.get("success", 0)
        failed = result.get("failed", 0)
        vc = result.get("video_count", 0)

        if total == 0:
            return

        self.status_label.setText(f"分发完成 — 成功 {success}/{total}")
        self.append_log(f"\n{'='*50}", "INFO")

        # 弹出删除确认对话框
        reply = QMessageBox.question(
            self,
            "🎬 分发完成",
            f"✅ 分发结果:\n\n"
            f"   设备: 成功 {success} 台 / 失败 {failed} 台 / 共 {total} 台\n"
            f"   视频: {vc} 个\n"
            f"   时间: {datetime.now().strftime('%H:%M:%S')}\n\n"
            f"是否删除本次已导入的视频文件？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            allocations = result.get("allocations", {})
            success_map = result.get("success_map", {})
            cleaner = FileCleaner(self.current_config, app_logger)
            cleaner.clean_uploaded_files(allocations, success_map)
            deleted = sum(len(v) for u, v in allocations.items() if success_map.get(u, False))
            self.append_log(f"✅ 已删除 {deleted} 个已导入的视频文件", "OK")
            # 刷新视频数量显示
            self.refresh_video_info()
        else:
            self.append_log("📌 已保留所有视频文件", "INFO")

        self.append_log("━━━ 可换下一批手机继续分发 ━━━", "INFO")
        self.progress_bar.setFormat(f"完成 — {total} 台设备")

    def on_error(self, error_msg: str):
        """致命错误"""
        self.running = False
        self.toggle_buttons(True)
        self.warning_banner.hide()
        self.append_log(error_msg, "ERR")
        self.status_label.setText("发生错误")
        QMessageBox.critical(self, "错误", error_msg)

    def on_warning(self, msg: str):
        """警告消息"""
        self.append_log(msg, "WARN")

    def on_open_log_dir(self):
        """打开日志目录"""
        import subprocess
        path = str(LOG_DIR.resolve())
        subprocess.run(["open", path])

    # ---- 窗口事件 ----

    def closeEvent(self, event):
        """关闭窗口时的处理"""
        if self.running and self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "确认退出",
                "分发任务正在进行中。\n确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.stop()
            self.worker.wait(3000)  # 等最多3秒
        event.accept()


# ============================================================
# 入口
# ============================================================

def main():
    # 高 DPI 支持
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    app = QApplication(sys.argv)
    app.setApplicationName("TK 视频分发工具")

    window = MainWindow()
    window.show()

    print("\n" + "=" * 50)
    print("  🎬 TK 视频分发工具 - 桌面版")
    print("=" * 50)
    print(f"  版本: v1.1.2")
    print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50 + "\n")

    exit_code = app.exec()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
