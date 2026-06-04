#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TK 视频自动分发工具 v3.0 (AFC 直传 + 增强照片库重建)
===================================================

不再依赖爱思助手 GUI，改用 afcclient C 原生协议栈直传 + 三重照片库重建。

核心流程：
  1. 设备检测 → 2. 视频分配（内容去重）→ 3. AFC 上传 → 4. 三重照片库重建

使用：
  python tk-distributor.py                         # 正常运行
  python tk-distributor.py --list-devices          # 仅列设备
  python tk-distributor.py --rebuild-only          # 仅重建照片库
  python tk-distributor.py --single file.mp4       # 导入单个文件
  python tk-distributor.py --legacy                # 旧版爱思 GUI 方案
"""

import os
import sys
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 导入核心模块
from tk_distributor_core import (
    load_config, LOG_DIR, CONFIG_FILE,
    DeviceManager, VideoAllocator, FileCleaner,
)

# 导入 AFC 上传 + 重建模块
import import_to_iphone

SCRIPT_DIR = Path(__file__).parent.resolve()


def setup_logging(config: dict) -> logging.Logger:
    """配置日志系统"""
    log_dir = config.get("log_dir_abs", LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("TKDistributor")
    logger.setLevel(getattr(logging, config.get("log_level", "INFO")))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(console_handler)

    today_str = time.strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(
        log_dir / f"distribute_{today_str}.log", encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s [%(funcName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(file_handler)

    return logger


class TKDistributorCLI:
    """主分发控制器 v3.0 (AFC 直传版)"""

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.device_manager = DeviceManager(logger)
        self.video_allocator = VideoAllocator(config, logger)
        self.file_cleaner = FileCleaner(config, logger)
        self.dry_run = config.get("dry_run", False)

    def run(self, list_only: bool = False, rebuild_only: bool = False,
            single_file: str = None, force_rebuild: bool = False):
        """执行分发流程"""
        print("\n" + "=" * 60)
        print("  🎬 TK 视频自动分发工具 v3.0 (AFC 直传)")
        print("=" * 60 + "\n")

        if self.dry_run:
            print("  ⚠️  DRY-RUN 模式 - 仅模拟，不实际操作\n")

        # 检测设备
        self.logger.info("正在检测 iOS 设备...")
        devices = self.device_manager.detect_devices()

        if not devices:
            self.logger.error("❌ 未检测到任何 iOS 设备！")
            self.logger.error("   请确认：")
            self.logger.error("   1. iPhone 已通过 USB 连接")
            self.logger.error("   2. iPhone 上已「信任此电脑」")
            return

        max_batch = self.config.get("max_devices_per_batch", 20)
        if len(devices) > max_batch:
            devices = devices[:max_batch]
            self.logger.info(f"⚠️ 设备超限，只处理前 {max_batch} 台")

        # 仅列设备
        if list_only:
            print(f"\n  当前连接 {len(devices)} 台设备:\n")
            for i, d in enumerate(devices, 1):
                print(f"  {i:2d}. {d['name']}  (UDID: {d['udid'][:16]}...)")
            print()
            return

        # 仅重建照片库
        if rebuild_only:
            self.logger.info(f"📸 仅重建照片库（不传文件）")
            for device in devices:
                udid = device["udid"]
                self.logger.info(f"  重建 {device['name']} 的照片库...")
                import_to_iphone.rebuild_photo_library(udid, force=force_rebuild)
            return

        # 单个文件模式
        if single_file:
            p = Path(single_file)
            if not p.exists():
                self.logger.error(f"❌ 文件不存在: {single_file}")
                return
            udid = devices[0]["udid"]
            self.logger.info(f"🎬 导入单个文件: {p.name}")
            self.logger.info(f"📱 设备: {devices[0]['name']} ({udid[:16]}...)")

            success = import_to_iphone.import_single_video(
                udid, p, force_rebuild=force_rebuild
            )
            if success:
                self.logger.info("✅ 导入成功！")
            else:
                self.logger.error("❌ 导入失败！")
            return

        # ====== 完整分发流程 ======
        self.logger.info(f"✅ 共检测到 {len(devices)} 台设备")

        # 分配视频
        self.logger.info("正在分配视频（内容去重）...")
        allocations = self.video_allocator.allocate(devices)
        if not allocations:
            self.logger.error("❌ 没有可分配的视频！")
            return

        total_videos = sum(len(v) for v in allocations.values())
        self.logger.info(f"📦 分配完成: {len(allocations)} 台设备 × "
                         f"{self.config.get('videos_per_device', 3)} 个 = {total_videos} 个视频")

        # 逐设备并发上传（max_workers 控制 USB 并发数）
        self.logger.info("\n📤 开始并发上传...")
        self.logger.info("-" * 40)

        success_map = {}
        success_devices = []

        def upload_single_device(device):
            """单设备上传任务（在线程池中执行）"""
            udid = device["udid"]
            device_ok = True
            local_results = []

            if udid not in allocations:
                return device, True, local_results

            videos = allocations[udid]

            if not self.dry_run and videos:
                results = import_to_iphone.upload_videos_batch(udid, videos)
                for vp, r in zip(videos, results):
                    if r:
                        self.logger.info(f"  ✅ {vp.name} → {r}")
                    else:
                        self.logger.error(f"  ❌ {vp.name} 上传失败")
                        device_ok = False
                local_results = results

            return device, device_ok, local_results

        # 并发上传（max_workers=3 避免 USB 总线压力过大）
        max_workers = min(3, len(devices))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(upload_single_device, device): device
                for device in devices
            }

            for i, future in enumerate(as_completed(futures)):
                device, device_ok, _ = future.result()
                udid = device["udid"]
                success_map[udid] = device_ok
                if device_ok:
                    success_devices.append(device)
                self.logger.info(f"  [{i+1}/{len(devices)}] {device['name']} 上传{'成功' if device_ok else '失败'}")

        # 阶段2：全部上传完后，顺序执行综合重建+重启
        # 每台一次连接做完删DB+重启，连接数减半，减少usbmuxd压力
        if not self.dry_run and success_devices:
            self.logger.info(f"\n📸 全部上传完成，综合重建+重启 {len(success_devices)} 台...")
            self.logger.info("-" * 40)

            restart_ok = []
            for i, device in enumerate(success_devices):
                udid = device["udid"]
                self.logger.info(f"  [{i+1}/{len(success_devices)}] {device['name']}: 重建+重启...")
                ok = import_to_iphone.rebuild_and_restart(udid)
                if ok:
                    restart_ok.append(device)
                time.sleep(2)

            if restart_ok:
                self.logger.info(f"\n⏳ 等待 {len(restart_ok)} 台重启完成（90秒）...")
                time.sleep(90)
                self.logger.info(f"✅ 等待完成！")

            # 90秒后USB稳定了，再试仍然失败的
            still_failed = [d for d in success_devices
                           if d["udid"] not in set(x["udid"] for x in restart_ok)]
            if still_failed:
                self.logger.info(f"Phase D: 90秒后重试 {len(still_failed)} 台（USB已稳定）...")
                for i, device in enumerate(still_failed):
                    udid = device["udid"]
                    ok = import_to_iphone.rebuild_and_restart(udid)
                    if ok:
                        restart_ok.append(device)
                        self.logger.info(f"  ✅ 重试成功")
                        time.sleep(5)
                    else:
                        self.logger.error(f"  ❌ {device['name']}: 彻底失败，请手动操作")

        # 标记已分发
        self.video_allocator.mark_as_distributed(allocations, success_map)

        # 清理已上传的文件
        if not self.dry_run:
            self.file_cleaner.clean_uploaded_files(allocations, success_map, dry_run=self.dry_run)

        # 输出总结
        self._print_summary(devices, allocations, success_map)

    def _print_summary(self, devices, allocations, success_map):
        success_count = sum(1 for s in success_map.values() if s)
        fail_count = len(success_map) - success_count
        total_videos = sum(len(v) for v in allocations.values())

        print("\n" + "=" * 60)
        print("  📊 分发完成 - 总结")
        print("=" * 60)
        print(f"  设备总数:   {len(devices)} 台")
        print(f"  成功设备:   {success_count} 台 ✅")
        print(f"  失败设备:   {fail_count} 台 ❌")
        print(f"  分发视频:   {total_videos} 个")
        print(f"  传输方式:   afcclient C原生协议栈（无需爱思助手）")
        print(f"  照片重建:   三重策略（JPEG诱导 + 通知 + Diagnostics）")
        print("=" * 60 + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="TK 视频自动分发工具 v3.0 (AFC 直传，不再需要爱思助手)"
    )

    # 主要模式
    parser.add_argument("--dry-run", action="store_true", help="模拟运行")
    parser.add_argument("--list-devices", "-l", action="store_true", help="仅列出设备")
    parser.add_argument("--rebuild-only", "-r", action="store_true",
                        help="仅重建照片库（不传文件）")
    parser.add_argument("--single", "-s", type=str,
                        help="导入单个视频文件")
    parser.add_argument("--legacy", action="store_true",
                        help="使用旧版爱思助手 GUI 方案（不推荐）")

    # 选项
    parser.add_argument("--force-rebuild", "-f", action="store_true",
                        help="强制重建 Photos.sqlite（极端，非必要不用）")
    parser.add_argument("--config", "-c", type=str, help="指定配置文件路径")

    args = parser.parse_args()

    # 处理配置文件
    config_path = args.config if args.config else None
    if config_path:
        global CONFIG_FILE
        CONFIG_FILE = Path(config_path)
        if not CONFIG_FILE.exists():
            print(f"[错误] 配置文件不存在: {config_path}")
            sys.exit(1)

    config = load_config()
    if args.dry_run:
        config["dry_run"] = True

    # 旧版爱思助手方案
    if args.legacy:
        print("⚠️  使用旧版爱思助手 GUI 方案（不推荐，坐标不稳定）")
        print("   建议改用新版 AFC 直传方案（默认）\n")
        from i4tools_remote import main as legacy_main
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != '--legacy']
        legacy_main()
        return

    logger = setup_logging(config)
    distributor = TKDistributorCLI(config, logger)

    distributor.run(
        list_only=args.list_devices,
        rebuild_only=args.rebuild_only,
        single_file=args.single,
        force_rebuild=args.force_rebuild,
    )


if __name__ == "__main__":
    main()
