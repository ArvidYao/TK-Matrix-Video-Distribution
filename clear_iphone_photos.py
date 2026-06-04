#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iPhone 相机胶卷清空模块
通过 AFC 批量删除 DCIM 目录中的媒体文件
"""

import asyncio
import time
from pathlib import Path
from typing import Optional


async def clear_camera_roll(udid: str,
                            media_types: Optional[set] = None,
                            dry_run: bool = False) -> dict:
    """
    清空 iPhone DCIM 目录中的媒体文件

    流程：
    1. AFC 连接设备
    2. 遍历 DCIM 下所有 *APPLE 子目录（100APPLE, 101APPLE...）
    3. 按类型过滤删除文件
    4. 返回删除统计

    Args:
        udid: 设备 UDID
        media_types: 要删除的扩展名集合，如 {'mp4', 'mov', 'm4v'}
                     传 None 则删除所有媒体文件
        dry_run: 只统计不删除

    Returns:
        dict: {"deleted": int, "failed": int, "files": list, "error": str|None}
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    if media_types is None:
        media_types = {'mp4', 'mov', 'm4v', 'jpg', 'jpeg',
                       'png', 'heic', 'gif', 'avi', 'heif'}

    deleted_count = 0
    failed_count = 0
    deleted_files = []

    try:
        lockdown = await create_using_usbmux(serial=udid)
        async with AfcService(lockdown) as afc:
            # 1. 列出 DCIM 下所有子目录
            try:
                dcim_dirs = await afc.listdir('DCIM')
            except Exception:
                return {"deleted": 0, "failed": 0, "files": [],
                        "error": "无法列出 DCIM 目录"}

            # 筛选出相册相关目录（100APPLE, 101APPLE 等）
            apple_dirs = [d for d in dcim_dirs
                         if d.endswith('APPLE') or
                         (d[0].isdigit() and 'APPLE' in d)]

            if not apple_dirs:
                # 兼容其他命名格式
                apple_dirs = [d for d in dcim_dirs if d[0].isdigit()]

            # 2. 遍历每个子目录
            for apple_dir in apple_dirs:
                remote_path = f'DCIM/{apple_dir}'
                try:
                    files = await afc.listdir(remote_path)
                except Exception:
                    continue

                for filename in files:
                    # 跳过目录
                    if '.' not in filename:
                        continue

                    ext = filename.rsplit('.', 1)[-1].lower()
                    if media_types and ext not in media_types:
                        continue

                    file_remote = f'{remote_path}/{filename}'

                    if dry_run:
                        deleted_count += 1
                        deleted_files.append(file_remote)
                        continue

                    try:
                        await afc.rm(file_remote)
                        deleted_count += 1
                        deleted_files.append(file_remote)
                    except Exception:
                        failed_count += 1

    except Exception as e:
        return {"deleted": 0, "failed": 1,
                "files": [], "error": str(e)}

    return {
        "deleted": deleted_count,
        "failed": failed_count,
        "files": deleted_files,
        "error": None,
    }


def clear_device_videos_sync(udid: str,
                              media_types: Optional[set] = None,
                              dry_run: bool = False) -> dict:
    """同步包装器，供 DistributeWorker 等同步上下文调用"""
    return asyncio.run(clear_camera_roll(udid, media_types, dry_run))
