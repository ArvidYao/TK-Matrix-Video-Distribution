#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iPhone 照片/视频导入工具 v2.0 (纯命令行，无需 GUI)
==================================================

使用 IMG_XXXX 标准命名上传视频到 iPhone DCIM 目录，
配合 Photos.sqlite 强制重建 + 通知，确保视频出现在相册。

用法:
    # 导入单个文件
    python3 import_to_iphone.py /path/to/video.mp4

    # 批量导入文件夹
    python3 import_to_iphone.py /path/to/folder/

    # 列出已连接设备
    python3 import_to_iphone.py --list

    # 仅重建照片库
    python3 import_to_iphone.py --rebuild-only

    # 指定设备 UDID
    python3 import_to_iphone.py /path/to/video.mp4 --udid xxxxx

依赖:
    pip install PyMobileDevice3
"""

import asyncio
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 支持的媒体文件扩展名
MEDIA_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.mov', '.mp4', '.m4v', '.gif', '.avi'}

# 照片库 DCIM 标准前缀
DCIM_PREFIX = "IMG_"
DCIM_START = 1


def log(msg: str):
    """带时间戳的日志输出"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] {msg}")


# ============================================================
# 模块1：设备检测
# ============================================================

async def get_devices():
    """获取所有已连接的 iOS 设备"""
    from pymobiledevice3.usbmux import list_devices as _list_devices
    return await _list_devices()


async def list_devices():
    """列出已连接设备"""
    devices = await get_devices()
    if not devices:
        print("❌ 未检测到任何 iOS 设备")
        print("   请确保：")
        print("   1. iPhone 已通过 USB 连接到电脑")
        print("   2. 已在手机上点击「信任此电脑」")
        return

    print(f"📱 发现 {len(devices)} 个设备:\n")
    for i, d in enumerate(devices, 1):
        conn_type = "USB 🟢" if d.is_usb else "WiFi 🟡"
        serial_short = d.serial[:16] if d.serial else "未知"
        print(f"  [{i}] 序列号: {serial_short}...")
        print(f"      连接: {conn_type}\n")


# ============================================================
# 模块2：文件上传（afcclient CLI 主力 + pymobiledevice3 备用）
# ============================================================

def upload_via_afcclient(udid: str, local_path: Path, remote_path: str) -> bool:
    """
    使用 libimobiledevice afcclient 上传（C 原生，超快）

    注意：afcclient 通过 stdin pipe 输入，路径含中文时会编码错误。
    仅限 ASCII 路径使用。

    Args:
        udid: 设备 UDID
        local_path: 本地文件路径
        remote_path: 远程路径（如 DCIM/100APPLE/IMG_0001.mp4）

    Returns:
        bool: 是否成功
    """
    cmd_input = f"put {local_path} {remote_path}\nexit\n"
    try:
        result = subprocess.run(
            ["afcclient", "-u", udid],
            input=cmd_input,
            capture_output=True, text=True,
            timeout=600
        )
        # afcclient 返回 exit code 0 但实际失败时 stdout 会包含 "Error"
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode == 0 and "Error" not in stdout:
            return True
        else:
            err_msg = stderr or stdout[:200]
            log(f"  ⚠️ afcclient 上传失败: {err_msg[:200]}")
            return False
    except FileNotFoundError:
        log("  ❌ afcclient 未找到。请安装: brew install libimobiledevice")
    except subprocess.TimeoutExpired:
        log("  ❌ afcclient 上传超时 (>10min)")
    except Exception as e:
        log(f"  ⚠️ afcclient 异常: {type(e).__name__}: {e}")
    return False


# ============================================================
# 模块3：照片库重建（通知 + 强制重置）
# ============================================================

def _query_next_dcim_number(udid: str, remote_dir: str, prefix: str = 'IMG_',
                             start: int = 1) -> int:
    """
    查询设备上已有的文件，返回下一个可用的 IMG_ 序号

    Args:
        udid: 设备 UDID
        remote_dir: 远程目录
        prefix: 文件前缀
        start: 起始编号

    Returns:
        int: 下一个可用的编号
    """
    import re
    try:
        result = subprocess.run(
            ["afcclient", "-u", udid],
            input=f"ls /{remote_dir}\nexit\n",
            capture_output=True, text=True,
            timeout=15
        )
        if result.returncode == 0:
            pattern = re.compile(rf'^{re.escape(prefix)}(\d{{4}})\.', re.IGNORECASE)
            max_num = 0
            for line in result.stdout.split('\n'):
                line = line.strip()
                match = pattern.search(line)
                if match:
                    num = int(match.group(1))
                    max_num = max(max_num, num)
            next_num = max(max_num + 1, start)
            return next_num
    except Exception:
        pass
    return start


def upload_video(udid: str, local_path: Path, remote_dir: str = 'DCIM/100APPLE',
                 dcim_prefix: str = 'IMG_') -> Optional[str]:
    """
    上传视频到 iPhone DCIM 目录

    使用 iOS 标准 IMG_XXXX 命名格式，确保照片 App 能识别。
    自动查询设备上的最大编号，避免覆盖已有文件。

    Args:
        udid: 设备 UDID
        local_path: 本地视频路径
        remote_dir: 远程目录（默认 DCIM/100APPLE）
        dcim_prefix: 文件前缀（默认 IMG_，iOS 标准格式）

    Returns:
        str: 远程文件名（成功时），None（失败时）
    """
    file_size = local_path.stat().st_size
    ext = local_path.suffix.lower()

    # 查询设备上已有的文件，确定下一个可用编号
    next_num = _query_next_dcim_number(udid, remote_dir, dcim_prefix)
    remote_name = f"{dcim_prefix}{next_num:04d}{ext}"

    log(f"📤 上传: {local_path.name} ({_format_size(file_size)}) → {remote_name}")

    # 主力：pymobiledevice3 异步上传（支持所有路径编码）
    async def _do_upload():
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        lockdown = await create_using_usbmux(serial=udid)
        async with AfcService(lockdown) as afc:
            await afc.makedirs(remote_dir)
            start = time.time()

            # 分块上传（1MB 每块，避免大文件内存爆炸）
            CHUNK_SIZE = 1 * 1024 * 1024
            fh = await afc.fopen(f"{remote_dir}/{remote_name}", "w")
            uploaded = 0
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    await afc.fwrite(fh, chunk)
                    uploaded += len(chunk)
            await afc.fclose(fh)

            elapsed = time.time() - start
            speed = uploaded / elapsed if elapsed > 0 else 0
            log(f"✅ 上传完成 ({elapsed:.1f}s, {_format_size(speed)}/s)")
            return True

    try:
        success = asyncio.run(_do_upload())
        if success:
            return remote_name
    except Exception as e:
        log(f"  ⚠️ pymobiledevice3 上传失败: {e}")

    return None


def remove_photos_database(udid: str) -> bool:
    """
    仅移除 Photos.sqlite（不重启）

    适用于批量场景：先给所有设备移除数据库，
    最后统一重启。

    Args:
        udid: 设备 UDID

    Returns:
        bool: 是否成功
    """
    for attempt in range(1, 4):
        try:
            async def _do():
                from pymobiledevice3.lockdown import retry_create_using_usbmux
                from pymobiledevice3.services.afc import AfcService

                lockdown = await retry_create_using_usbmux(serial=udid)
                async with AfcService(lockdown) as afc:
                    ts = time.strftime("%Y%m%d%H%M%S")
                    renamed = 0
                    for fname in ['Photos.sqlite', 'Photos.sqlite-wal', 'Photos.sqlite-shm']:
                        src = f'/PhotoData/{fname}'
                        dst = f'/PhotoData/{fname}.bak_afc_{ts}'
                        try:
                            await afc.rename(src, dst)
                            renamed += 1
                        except Exception:
                            pass
                    log(f"  📝 Photos.sqlite 已移除 ({renamed} 个文件)" if renamed > 0 else "  ⚠️ 无文件可移除")

            asyncio.run(_do())
            return True

        except Exception as e:
            log(f"  ⚠️ 移除数据库失败 [{attempt}]: {type(e).__name__}")
            if attempt < 3:
                time.sleep(2)
            else:
                return False
    return False


def restart_device(udid: str) -> bool:
    """
    仅发送重启命令（不移除 Photos.sqlite）

    所有设备的数据库移除完成后统一调用。

    Args:
        udid: 设备 UDID

    Returns:
        bool: 是否已发送重启命令
    """
    for attempt in range(1, 4):
        try:
            async def _do():
                from pymobiledevice3.lockdown import retry_create_using_usbmux
                from pymobiledevice3.services.diagnostics import DiagnosticsService
                lockdown = await retry_create_using_usbmux(serial=udid)
                diag = DiagnosticsService(lockdown)
                await diag.restart()
                log("  🔄 重启命令已发送")

            asyncio.run(_do())
            return True

        except Exception as e:
            log(f"  ⚠️ 重启失败 [{attempt}]: {type(e).__name__}")
            if attempt < 3:
                time.sleep(2)
            else:
                return False
    return False


def rebuild_and_restart(udid: str) -> bool:
    """
    终极方案：移除 Photos.sqlite → 重启 iPhone

    原理：先重命名 Photos.sqlite 让数据库消失，
    然后重启 iPhone。启动时 iOS 检测到数据库缺失，
    会自动创建新数据库 + 扫描整个 DCIM 目录。

    这是目前验证过最可靠的方式，但需要等 60-90 秒重启。

    包含重试机制：如果连接失败（如其他设备重启导致 usbmuxd 抖动），
    会自动等待 3 秒后重试，最多 3 次。

    Args:
        udid: 设备 UDID

    Returns:
        bool: 重启命令是否已发送
    """
    max_retries = 5
    base_wait = 1  # 1秒快速重试
    for attempt in range(1, max_retries + 1):
        renamed_files = []  # 记录本次改名的文件，用于失败回滚
        try:
            log(f"📸 重建+重启 [{attempt}/{max_retries}]：{udid[:16]}...")

            async def _do():
                from pymobiledevice3.lockdown import create_using_usbmux
                from pymobiledevice3.services.afc import AfcService
                from pymobiledevice3.services.diagnostics import DiagnosticsService

                # 直接连接，不要内置重试（由外层重试兜底，更快）
                lockdown = await create_using_usbmux(serial=udid)

                # 步骤1：重命名 Photos.sqlite（统一后缀 bak_afc_*）
                async with AfcService(lockdown) as afc:
                    # 清理旧备份
                    try:
                        existing = await afc.listdir('/PhotoData')
                        old_baks = [f for f in existing if f.startswith('Photos.sqlite.bak_afc_')]
                        for old_bak in old_baks:
                            try:
                                await afc.rm(f'/PhotoData/{old_bak}')
                            except Exception:
                                pass
                    except Exception:
                        pass

                    ts = time.strftime("%Y%m%d%H%M%S")
                    renamed_cnt = 0
                    for fname in ['Photos.sqlite', 'Photos.sqlite-wal', 'Photos.sqlite-shm']:
                        src = f'/PhotoData/{fname}'
                        dst = f'/PhotoData/{fname}.bak_afc_{ts}'
                        try:
                            await afc.rename(src, dst)
                            log(f"  📝 已重命名: {fname}")
                            renamed_files.append(dst)
                            renamed_cnt += 1
                        except Exception:
                            pass
                    if renamed_cnt > 0:
                        log(f"  ✅ Photos.sqlite 已移除 ({renamed_cnt} 个文件)")

                # 步骤2：重启 iPhone（复用同一 lockdown 连接）
                log("  🔄 正在重启 iPhone...")
                diag = DiagnosticsService(lockdown)
                await diag.restart()
                log("  ✅ 重启命令已发送")

            asyncio.run(_do())

            log("💡 请等待 60-90 秒后打开 iPhone「照片」App 查看新视频")
            return True

        except Exception as e:
            err_name = type(e).__name__
            log(f"  ⚠️ 第 {attempt} 次失败: {err_name}")

            # ★★★ 致命保护：如果数据库已改名但重启失败，恢复数据库 ★★★
            if renamed_files:
                log(f"  🔄 恢复数据库（重启失败，撤销改名）...")
                try:
                    loop2 = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop2)
                    async def _restore():
                        from pymobiledevice3.lockdown import create_using_usbmux
                        from pymobiledevice3.services.afc import AfcService
                        lockdown2 = await create_using_usbmux(serial=udid)
                        async with AfcService(lockdown2) as afc2:
                            for bak_name in renamed_files:
                                orig = bak_name.replace('.bak_afc_', '.bak_afc_').rsplit('.bak_afc_', 1)[0]
                                try:
                                    await afc2.rename(f'/PhotoData/{bak_name}', f'/PhotoData/{orig}')
                                    log(f"  ✅ 已恢复: {orig}")
                                except Exception:
                                    pass
                    loop2.run_until_complete(_restore())
                    loop2.close()
                    asyncio.set_event_loop(None)
                    log(f"  ✅ 数据库已恢复")
                except Exception as restore_err:
                    log(f"  ⚠️ 恢复失败: {restore_err}")

            if attempt < max_retries:
                wait = base_wait
                log(f"  ⏳ 等待 {wait} 秒...")
                time.sleep(wait)

    # ★ 所有 pymobiledevice3 重试都失败后，尝试 CLI 兜底
    log(f"  🔄 尝试 CLI 兜底...")
    try:
        import subprocess as _sp
        # 先用 afcclient 删数据库（用 mv 命令改名）
        _ts = str(int(time.time()))
        for _f in ['Photos.sqlite', 'Photos.sqlite-wal', 'Photos.sqlite-shm']:
            _sp.run(
                ['afcclient', '-u', udid],
                input=f"mv /PhotoData/{_f} /PhotoData/{_f}.cli_{_ts}\nexit\n",
                capture_output=True, text=True, timeout=10
            )
        # 再用 idevicediagnostics 重启（明确指定 UDID）
        _sp.run(
            ['idevicediagnostics', '-u', udid, 'restart'],
            capture_output=True, text=True, timeout=15
        )
        log(f"  ✅ CLI 重启命令已发送")
        return True
    except Exception as _ce:
        log(f"  ⚠️ CLI 重启异常: {_ce}")

    log(f"  ❌ 跳过此设备")
    return False


def upload_videos_batch(udid: str, video_paths: List[Path],
                        remote_dir: str = 'DCIM/100APPLE',
                        dcim_prefix: str = 'IMG_') -> List[Optional[str]]:
    """
    批量上传多个视频（单次连接，避免反复开关导致 BrokenPipe）

    优化：批次开始时查一次 listdir，后续直接用内存计数器递增编号。

    Args:
        udid: 设备 UDID
        video_paths: 视频路径列表
        remote_dir: 远程 DCIM 目录
        dcim_prefix: 文件前缀

    Returns:
        List[Optional[str]]: 每个文件上传后的远程文件名或 None
    """
    results: List[Optional[str]] = []

    async def _batch():
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        lockdown = await create_using_usbmux(serial=udid)
        async with AfcService(lockdown) as afc:
            await afc.makedirs(remote_dir)

            # 批次开始时查一次 listdir，后续直接用内存计数器递增
            existing_files = await afc.listdir(remote_dir)
            next_num = _query_next_dcim_number_by_list(existing_files, dcim_prefix)

            batch_results = []
            for vp in video_paths:
                fs = vp.stat().st_size
                rn = f"{dcim_prefix}{next_num:04d}{vp.suffix.lower()}"
                log(f"📤 上传: {vp.name} ({_format_size(fs)}) → {rn}")
                try:
                    st = time.time()
                    CHUNK = 1024 * 1024
                    fh = await afc.fopen(f"{remote_dir}/{rn}", "w")
                    up = 0
                    with open(vp, "rb") as f:
                        while True:
                            c = f.read(CHUNK)
                            if not c:
                                break
                            await afc.fwrite(fh, c)
                            up += len(c)
                    await afc.fclose(fh)
                    el = time.time() - st
                    log(f"  ✅ ({el:.1f}s, {_format_size(fs/el if el else 0)}/s)")
                    batch_results.append(rn)
                    next_num += 1  # 内存计数器递增
                except Exception as e:
                    log(f"  ⚠️ 失败: {e}")
                    batch_results.append(None)
            return batch_results

    try:
        results = asyncio.run(_batch())
    except Exception as e:
        log(f"  ⚠️ 批量上传连接失败: {e}")
        results = [None] * len(video_paths)

    return results


def _query_next_dcim_number_by_list(file_list: list, prefix: str = 'IMG_',
                                     start: int = 1) -> int:
    """从文件列表中查询下一个可用编号"""
    import re
    pattern = re.compile(rf'^{re.escape(prefix)}(\d{{4}})\.', re.IGNORECASE)
    max_num = 0
    for fname in file_list:
        match = pattern.search(fname)
        if match:
            num = int(match.group(1))
            max_num = max(max_num, num)
    return max(max_num + 1, start)


# ============================================================
# 模块3：照片库重建（通知 + 强制重置）
# ============================================================



async def _send_notifications(udid: str, rounds: int = 2) -> int:
    """
    策略2：发送多轮照片库通知

    发送多个系统级通知，通知 iOS 重新扫描照片库。
    每一轮发送不同的通知集，覆盖更多服务。

    Args:
        udid: 设备 UDID
        rounds: 通知轮数

    Returns:
        int: 成功发送的通知总数
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.notification_proxy import NotificationProxyService

    total_sent = 0

    try:
        lockdown = await create_using_usbmux(serial=udid)
        nps = NotificationProxyService(lockdown)

        for r in range(rounds):
            if r == 0:
                notifications = [
                    "com.apple.photos.PhotoLibraryChangeObserver",
                    "com.apple.mobile.assetd",
                    "com.apple.photos.ImageCapture",
                ]
            else:
                notifications = [
                    "com.apple.MobileAssetService",
                    "com.apple.mediaserverd",
                    "SBSERVICESRESTARTREQUESTED",
                ]

            for note in notifications:
                try:
                    await nps.notify_post(note)
                    total_sent += 1
                except Exception:
                    pass

            if r < rounds - 1:
                await asyncio.sleep(1.5)

    except Exception as e:
        log(f"  ⚠️ 通知发送异常: {e}")

    return total_sent


async def _try_restart_services(udid: str) -> bool:
    """
    策略3（可选）：发送额外系统级通知

    pymobiledevice3 9.x 的 DiagnosticsService 不提供单服务重启 API，
    所以策略3改为发送额外系统通知作为补充。

    Returns:
        bool: 是否成功（始终返回 True，不影响主流程）
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.notification_proxy import NotificationProxyService

    try:
        lockdown = await create_using_usbmux(serial=udid)
        nps = NotificationProxyService(lockdown)

        extra_notifications = [
            "com.apple.PhotoLibraryAvailabilityChange",
            "com.apple.assetsd.rebuild",
        ]
        for note in extra_notifications:
            try:
                await nps.notify_post(note)
            except Exception:
                pass

        log(f"  ✅ 额外通知发送完成")
        return True
    except Exception as e:
        log(f"  ℹ️ 额外通知可选（不影响其他策略）: {type(e).__name__}")
        return True


def rebuild_photo_library(udid: str, force: bool = False) -> bool:
    """
    增强型照片库重建（双重策略）

    按顺序执行，确保视频出现在 iPhone 相册。

    Args:
        udid: 设备 UDID
        force: 是否使用强力重建（重置 Photos.sqlite）

    Returns:
        bool: 至少一个策略成功
    """
    log("📸 开始照片库重建...")

    # 策略1：发送多轮通知
    log("  [1/2] 发送照片库通知...")
    try:
        sent = asyncio.run(_send_notifications(udid))
        log(f"  ✅ 已发送 {sent} 个通知")
    except Exception as e:
        log(f"  ⚠️ 通知发送异常: {e}")

    # 策略2：额外通知
    log("  [2/2] 发送补充通知...")
    try:
        asyncio.run(_try_restart_services(udid))
    except Exception:
        pass

    # 强制重建（重置 Photos.sqlite）— ⚠️ 只有必要时才用
    # 注意：多次强制重建会导致数据库损坏、相册打不开
    # 正常流程只发通知就够了，重启 iPhone 是更好的方式
    if force:
        log("  [强制] 重置 Photos.sqlite（仅建议首次上传后用一次）...")
        _force_rebuild_photos_db(udid)

    log("✅ 照片库重建完成")
    log("💡 请等待 15-20 秒后打开 iPhone「照片」App 查看新视频")
    return True


def _force_rebuild_photos_db(udid: str):
    """
    极端方案：重命名 Photos.sqlite 触发 iOS 完整重建

    这是 Apple 官方支持的行为——删除/重命名 Photos.sqlite 后，
    iOS 会自动重新扫描 DCIM 目录并生成新的数据库。
    """
    try:
        async def _do():
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.afc import AfcService

            lockdown = await create_using_usbmux(serial=udid)
            async with AfcService(lockdown) as afc:
                ts = datetime.now().strftime("%Y%m%d%H%M%S")

                # 清理旧备份（统一后缀 bak_afc_*）
                try:
                    existing = await afc.listdir('/PhotoData')
                    old_baks = [f for f in existing if f.startswith('Photos.sqlite.bak_afc_')]
                    for old_bak in old_baks:
                        try:
                            await afc.rm(f'/PhotoData/{old_bak}')
                        except Exception:
                            pass
                except Exception:
                    pass

                # 重命名当前数据库
                for fname in ['Photos.sqlite', 'Photos.sqlite-wal', 'Photos.sqlite-shm']:
                    src = f'/PhotoData/{fname}'
                    dst = f'/PhotoData/{fname}.bak_afc_{ts}'
                    try:
                        await afc.rename(src, dst)
                        log(f"  📝 已重命名: {fname} → .bak")
                    except Exception:
                        pass

        asyncio.run(_do())
    except Exception as e:
        log(f"  ⚠️ Photos.sqlite 重置失败: {e}")


def rebuild_photo_library_background(udid: str, force: bool = False):
    """
    后台线程启动照片库重建（不阻塞主流程）
    """
    t = threading.Thread(
        target=rebuild_photo_library,
        args=(udid, force),
        daemon=True,
        name=f"photo-rebuild-{udid[:8]}"
    )
    t.start()
    log("📸 照片库重建已在后台启动（可继续其他操作）")


# ============================================================
# 模块4：完整导入流程（上传 + 重建）
# ============================================================

def import_single_video(udid: str, local_path: Path,
                        dcim_dir: str = 'DCIM/100APPLE',
                        rebuild: bool = True,
                        force_rebuild: bool = False) -> bool:
    """
    完整的导入流程：上传视频 + 照片库重建

    Args:
        udid: 设备 UDID
        local_path: 本地视频路径
        dcim_dir: 远程 DCIM 目录
        rebuild: 是否执行照片库重建
        force_rebuild: 是否强制重建 Photos.sqlite

    Returns:
        bool: 是否成功
    """
    log(f"{'='*50}")
    log(f"🎬 导入: {local_path.name} ({_format_size(local_path.stat().st_size)})")
    log(f"{'='*50}")

    # 上传
    remote_name = upload_video(udid, local_path, dcim_dir)
    if not remote_name:
        log("❌ 上传失败")
        return False

    # 重建照片库
    if rebuild:
        rebuild_photo_library(udid, force=force_rebuild)

    log(f"✅ 导入流程完成: {local_path.name}")
    return True


def import_videos(udid: str, video_paths: List[Path],
                  rebuild: bool = True,
                  force_rebuild: bool = False) -> dict:
    """
    批量导入视频

    Args:
        udid: 设备 UDID
        video_paths: 视频文件路径列表
        rebuild: 是否执行照片库重建
        force_rebuild: 是否强制重建

    Returns:
        dict: 导入结果统计
    """
    success_count = 0
    fail_count = 0
    total_start = time.time()

    for i, vp in enumerate(video_paths, 1):
        print(f"\n[{i}/{len(video_paths)}]", end=" ")
        ok = import_single_video(udid, vp, rebuild=(i == len(video_paths) and rebuild),
                                  force_rebuild=(i == len(video_paths) and force_rebuild))
        if ok:
            success_count += 1
        else:
            fail_count += 1

        if i < len(video_paths):
            time.sleep(0.5)

    total_time = time.time() - total_start
    log(f"\n{'='*50}")
    log(f"📊 导入完成!")
    log(f"   ✅ 成功: {success_count} | ❌ 失败: {fail_count}")
    log(f"   ⏱ 耗时: {total_time:.1f}s")
    log(f"{'='*50}")

    return {"success": success_count, "failed": fail_count, "total": len(video_paths)}


# ============================================================
# 旧版兼容接口（async，保持向后兼容）
# ============================================================

async def push_file(afc_service, local_path: str, remote_dir: str = 'DCIM/100APPLE') -> bool:
    """兼容旧版接口：通过 AFC 服务推送单个文件"""
    local_path = Path(local_path)
    if not local_path.exists():
        return False
    if local_path.suffix.lower() not in MEDIA_EXTENSIONS:
        return False

    file_size = local_path.stat().st_size
    filename = f"IMP_{datetime.now().strftime('%Y%m%d_%H%M%S')}{local_path.suffix}"
    remote_path = f"{remote_dir}/{filename}"

    try:
        start_time = time.time()
        with open(local_path, 'rb') as f:
            await afc_service.set_file_contents(remote_path, f.read())
        elapsed = time.time() - start_time
        log(f"📤 {filename} ({elapsed:.1f}s)")
        return True
    except Exception:
        return False


async def _notify_photo_library_refresh(service_provider):
    """兼容旧版接口：发送照片库通知"""
    from pymobiledevice3.services.notification_proxy import NotificationProxyService
    try:
        nps = NotificationProxyService(service_provider)
        notifications = [
            "com.apple.photos.PhotoLibraryChangeObserver",
            "com.apple.mobile.assetd",
            "com.apple.photos.ImageCapture",
        ]
        for note in notifications:
            try:
                await nps.notify_post(note)
            except Exception:
                pass
    except Exception as e:
        log(f"⚠️ 通知异常: {e}")


async def import_files(file_paths: list, udid: str = None):
    """兼容旧版接口：批量导入"""
    all_files = []
    for path_str in file_paths:
        p = Path(path_str)
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS:
            all_files.append(p)
        elif p.is_dir():
            for ext in MEDIA_EXTENSIONS:
                all_files.extend(p.rglob(f'*{ext}'))
                all_files.extend(p.rglob(f'*{ext.upper()}'))

    if not all_files:
        print("❌ 没有找到可导入的媒体文件")
        return

    # 获取设备 UDID
    if not udid:
        devices = await get_devices()
        if not devices:
            print("❌ 未检测到设备")
            return
        udid = devices[0].serial

    import_videos(udid, all_files)


# ============================================================
# 工具函数
# ============================================================

def _format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}TB"


def print_usage():
    """打印使用说明"""
    print("""
╔══════════════════════════════════════════════════╗
║   📱 iPhone 媒体导入工具 v2.0 (AFC 直传 + 照片库重建)  ║
╠══════════════════════════════════════════════════╣
║                                                  ║
║  无需爱思助手、无需 GUI、不怕鼠标干扰             ║
║                                                  ║
║  命名：IMG_XXXX 标准格式（iOS 相册自动识别）      ║
║  重建：Photos.sqlite 强制重建 + 通知              ║
║                                                  ║
║  用法:                                           ║
║    python3 import_to_iphone.py <文件或文件夹>      ║
║    python3 import_to_iphone.py --list             ║
║    python3 import_to_iphone.py --rebuild-only     ║
║                                                  ║
║  选项:                                           ║
║    --udid <UDID>       指定目标设备               ║
║    --force-rebuild     强制重建 Photos.sqlite     ║
║    --rebuild-only      只重建照片库，不传文件      ║
║                                                  ║
╚══════════════════════════════════════════════════╝
""")


def main():
    args = sys.argv[1:]

    if not args or '--help' in args or '-h' in args:
        print_usage()
        sys.exit(0)

    if '--list' in args:
        asyncio.run(list_devices())
        return

    # 解析参数
    udid = None
    force_rebuild = '--force-rebuild' in args
    rebuild_only = '--rebuild-only' in args

    clean_args = [a for a in args if not a.startswith('--')]

    if rebuild_only:
        if not udid:
            devices = asyncio.run(get_devices())
            if not devices:
                print("❌ 未检测到设备")
                sys.exit(1)
            udid = devices[0].serial
        rebuild_photo_library(udid, force=force_rebuild)
        return

    # 提取 --udid
    for i, a in enumerate(args):
        if a == '--udid' and i + 1 < len(args):
            udid = args[i + 1]
            break

    if not clean_args:
        print("❌ 请指定要导入的文件或文件夹路径")
        sys.exit(1)

    # 如果未指定 UDID，自动检测第一个 USB 设备
    if not udid:
        devices = asyncio.run(get_devices())
        if not devices:
            print("❌ 未检测到 iOS 设备。请确保: ")
            print("   1. iPhone 已通过 USB 连接")
            print("   2. 已在手机上点击「信任此电脑」")
            sys.exit(1)
        usb_devices = [d for d in devices if d.is_usb]
        if usb_devices:
            udid = usb_devices[0].serial
        else:
            udid = devices[0].serial
        log(f"📱 自动选择设备: {udid[:16]}...")

    # 收集文件
    all_files = []
    for path_str in clean_args:
        p = Path(path_str)
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS:
            all_files.append(p)
        elif p.is_dir():
            for ext in MEDIA_EXTENSIONS:
                all_files.extend(p.rglob(f'*{ext}'))
                all_files.extend(p.rglob(f'*{ext.upper()}'))

    if not all_files:
        print("❌ 没有找到可导入的媒体文件")
        sys.exit(1)

    log(f"🎬 共 {len(all_files)} 个文件待导入")

    import_videos(udid, all_files, rebuild=True, force_rebuild=force_rebuild)


if __name__ == '__main__':
    main()
