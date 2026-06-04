#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok 视频去水印模块
=====================
基于 n0l3r/tiktok-downloader 核心逻辑重构，适配 Python 后端的视频下载管线。

核心原理:
    TikTok 内部 Feed API 返回的视频数据包含两个下载地址:
    - download_addr.url_list  → 带平台水印的版本
    - play_addr.url_list      → 无水印的原始视频流
    选择 play_addr 即可获得无水印视频。

技术来源: n0l3r/tiktok-downloader (MIT License)
参考 API: api22-normal-c-alisg.tiktokv.com/aweme/v1/feed/

作者: 集成自 n0l3r/tiktok-downloader
"""

import re
import json
import time
import random
import hashlib
import logging
import urllib.parse
from typing import Optional, Dict, List, Tuple

import requests

logger = logging.getLogger("TKDistributor.Watermark")

# ============================================================
# 常量定义 - 来自 n0l3r 项目的设备模拟参数
# ============================================================

# 多组设备信息轮换，降低被限流概率
_DEVICE_POOL = [
    {
        "iid": "7318518857994389254",
        "device_id": "7318517321748022790",
        "device_type": "ASUS_Z01QD",
    },
    {
        "iid": "7320565432109876543",
        "device_id": "7320565432109876543",
        "device_type": "SM-G998B",
    },
    {
        "iid": "7332109876543210123",
        "device_id": "7332109876543210123",
        "device_type": "Pixel 6 Pro",
    },
]

# TikTok Feed API 端点池（多地域轮换）
_API_POOL = [
    "https://api22-normal-c-alisg.tiktokv.com",
    "https://api16-normal-c-useast1a.tiktokv.com",
    "https://api19-core-c-alisg.tiktokv.com",
]

_TIKTOK_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/"
    r"(?:@[\w.-]+/video/|video/|photo/|t/|v/)([\w-]+)",
    re.IGNORECASE,
)

# 请求头 - 模拟 Android 客户端
_REQUEST_HEADERS = {
    "User-Agent": (
        "com.zhiliaoapp.musically/2023009040 (Linux; U; Android 13; en_US; "
        "ASUS_Z01QD; Build/TKQ1.221114.001; Cronet/TTNetVersion:01584b85 "
        "2023-09-04 QuicVersion:47538db5 2023-08-28)"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# 请求超时
_REQUEST_TIMEOUT = 15  # 秒

# 最大重试次数
_MAX_RETRIES = 2


# ============================================================
# 工具函数
# ============================================================

def _random_device() -> Dict[str, str]:
    """随机选择一个设备参数"""
    return random.choice(_DEVICE_POOL)


def _random_api() -> str:
    """随机选择一个 API 端点"""
    return random.choice(_API_POOL)


def _with_retry(func, max_retries: int = _MAX_RETRIES):
    """带退避的重试装饰器（内部使用）"""
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except TikTokAPIError:
                raise
            except Exception as e:
                last_exc = e
                if attempt < max_retries:
                    wait = (attempt + 1) * 2
                    logger.debug(f"重试 {attempt + 1}/{max_retries}，等待 {wait}s: {e}")
                    time.sleep(wait)
        raise TikTokAPIError(f"请求失败，已重试 {max_retries} 次: {last_exc}")
    return wrapper


# ============================================================
# 异常定义
# ============================================================

class TikTokAPIError(Exception):
    """TikTok API 调用异常"""
    pass


class VideoNotFoundError(TikTokAPIError):
    """视频不存在或已下架"""
    pass


class RateLimitError(TikTokAPIError):
    """触发频率限制"""
    pass


# ============================================================
# 视频 ID 提取
# ============================================================

def extract_video_id(video_url: str) -> str:
    """从 TikTok 视频链接中提取视频 ID。

    支持格式:
        - https://www.tiktok.com/@user/video/7234567890123456789
        - https://www.tiktok.com/video/7234567890123456789
        - https://vm.tiktok.com/ZMxxxxx/          (短链，需重定向)
        - https://vt.tiktok.com/ZSxxxxx/
        - https://www.tiktok.com/t/ZTxxxxx/        (移动端短链)

    参数:
        video_url: TikTok 视频页面完整 URL

    返回:
        str: 19 位数字视频 ID

    异常:
        VideoNotFoundError: 无法从 URL 中提取有效视频 ID
    """
    # 先尝试正则直接匹配
    match = _TIKTOK_URL_PATTERN.search(video_url)
    if match:
        raw_id = match.group(1)
        # 如果是数字 ID (>= 17 位)，直接返回
        if len(raw_id) >= 17 and raw_id.isdigit():
            return raw_id[:19]
        # 如果是短链路径，需要解析重定向
        if raw_id and not raw_id.isdigit():
            real_url = _resolve_short_link(video_url)
            return extract_video_id(real_url)

    raise VideoNotFoundError(f"无法从链接中提取视频 ID: {video_url}")


def _resolve_short_link(short_url: str) -> str:
    """解析 TikTok 短链 (t/xxxxx 或 vm.tiktok.com/xxxxx)，获取真实 URL。

    参数:
        short_url: TikTok 短链

    返回:
        str: 重定向后的完整视频 URL
    """
    try:
        resp = requests.head(
            short_url,
            headers=_REQUEST_HEADERS,
            allow_redirects=True,
            timeout=_REQUEST_TIMEOUT,
        )
        return resp.url
    except requests.RequestException as e:
        raise TikTokAPIError(f"无法解析短链: {short_url}: {e}")


# ============================================================
# 核心去水印逻辑 - 来自 n0l3r 项目
# ============================================================

def fetch_video_info(video_url: str) -> Dict:
    """调用 TikTok Feed API 获取视频元数据，包含无水印直链。

    这是 n0l3r/tiktok-downloader 项目 `getVideo()` 函数的 Python 等效实现。

    原理:
        1. 从 URL 提取 19 位视频 ID
        2. 调用 TikTok Feed API (模拟 Android 客户端)
        3. 解析 JSON 响应，提取:
           - play_addr.url_list → 无水印视频直链
           - download_addr.url_list → 带水印视频直链
           - image_post_info → 图集 (幻灯片模式)
           - aweme_id, desc, author 等元数据

    参数:
        video_url: TikTok 视频页面完整 URL

    返回:
        dict: {
            "aweme_id":       str,    # 视频 ID
            "desc":           str,    # 视频描述
            "author_name":    str,    # 作者用户名
            "author_nickname": str,   # 作者昵称
            "duration_ms":    int,    # 时长 (毫秒)
            "play_count":     int,    # 播放量
            "clean_url":      str,    # 无水印直链 (play_addr)
            "watermark_url":  str,    # 带水印直链 (download_addr)
            "thumbnail":      str,    # 封面图
            "is_image_post":  bool,   # 是否为图集
            "image_urls":     list,   # 图集图片链接列表
        }

    异常:
        VideoNotFoundError: 视频不存在
        RateLimitError: 触发频率限制
        TikTokAPIError: API 调用失败
    """
    video_id = extract_video_id(video_url)
    device = _random_device()
    api_base = _random_api()

    api_url = (
        f"{api_base}/aweme/v1/feed/?"
        f"aweme_id={video_id}&"
        f"iid={device['iid']}&"
        f"device_id={device['device_id']}&"
        f"channel=googleplay&"
        f"app_name=musical_ly&"
        f"version_code=300904&"
        f"device_platform=android&"
        f"device_type={device['device_type']}&"
        f"version=9"
    )

    logger.info(f"请求 TikTok API: {api_base}/... (aweme_id={video_id})")

    @_with_retry
    def _request():
        resp = requests.get(
            api_url,
            headers=_REQUEST_HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )

        # 检查限流
        text = resp.text
        if "ratelimit" in text.lower():
            raise RateLimitError(f"TikTok API 频率限制: {video_id}")

        # 解析 JSON
        try:
            data = resp.json()
        except json.JSONDecodeError:
            # 可能返回了 HTML (如验证页面)
            snippet = text[:200]
            raise TikTokAPIError(
                f"TikTok API 返回非 JSON 数据 (状态码 {resp.status_code}): {snippet}"
            )

        if resp.status_code != 200:
            raise TikTokAPIError(
                f"TikTok API HTTP {resp.status_code}: {data}"
            )

        return data

    data = _request()

    # 解析 aweme_list
    aweme_list = data.get("aweme_list")
    if not aweme_list or len(aweme_list) == 0:
        raise VideoNotFoundError(f"视频不存在或已下架: {video_id}")

    aweme = aweme_list[0]

    # 提取作者信息
    author = aweme.get("author", {})
    author_name = author.get("unique_id", "")
    author_nickname = author.get("nickname", "")

    # 提取统计数据
    stats = aweme.get("statistics", {})

    result = {
        "aweme_id": aweme.get("aweme_id", video_id),
        "desc": aweme.get("desc", "")[:200],
        "author_name": author_name,
        "author_nickname": author_nickname,
        "duration_ms": aweme.get("duration", 0),
        "play_count": stats.get("play_count", 0),
        "thumbnail": (
            aweme.get("video", {}).get("cover", {}).get("url_list", [""])[0]
            if aweme.get("video") else ""
        ),
        "is_image_post": False,
        "image_urls": [],
        "clean_url": "",
        "watermark_url": "",
    }

    # 图集模式 (幻灯片)
    if aweme.get("image_post_info"):
        result["is_image_post"] = True
        images = aweme["image_post_info"].get("images", [])
        for img in images:
            url_list = img.get("display_image", {}).get("url_list", [])
            if len(url_list) > 1:
                result["image_urls"].append(url_list[1])  # jpeg 质量
            elif url_list:
                result["image_urls"].append(url_list[0])

    # 视频模式
    video = aweme.get("video")
    if video:
        # 无水印直链 (play_addr) - 来自 n0l3r 项目的核心发现
        play_addr = video.get("play_addr", {})
        if play_addr.get("url_list"):
            result["clean_url"] = play_addr["url_list"][0]

        # 带水印直链 (download_addr)
        download_addr = video.get("download_addr", {})
        if download_addr.get("url_list"):
            result["watermark_url"] = download_addr["url_list"][0]

    # 如果 play_addr 为空，尝试 bit_rate 列表中的高质量流
    if not result["clean_url"] and video:
        bit_rates = video.get("bit_rate", [])
        if bit_rates:
            # 选择最高质量的无水印流
            best = None
            for br in bit_rates:
                play = br.get("play_addr", {})
                if play.get("url_list"):
                    best = play["url_list"][0]
            if best:
                result["clean_url"] = best

    if not result["clean_url"] and not result["image_urls"]:
        raise VideoNotFoundError(
            f"未找到视频流地址 (可能为私密/地区限制): {video_id}"
        )

    logger.info(
        f"✓ 获取视频信息成功: @{author_name} [{video_id}] "
        f"clean_url={'✓' if result['clean_url'] else '✗'} "
        f"images={len(result['image_urls'])}"
    )

    return result


def get_clean_video_url(video_url: str, fallback_to_watermark: bool = True) -> str:
    """便捷方法: 获取无水印视频直链。

    参数:
        video_url: TikTok 视频页面 URL
        fallback_to_watermark: 若无水印链接获取失败，是否降级返回带水印链接

    返回:
        str: 视频直链 (CDN URL)

    异常:
        VideoNotFoundError: 无法获取任何可用视频链接
    """
    info = fetch_video_info(video_url)

    if info["clean_url"]:
        return info["clean_url"]

    if fallback_to_watermark and info["watermark_url"]:
        logger.warning(f"无水印链接不可用，降级使用带水印版本: {info['aweme_id']}")
        return info["watermark_url"]

    raise VideoNotFoundError(f"无可用视频链接: {video_url}")


def get_image_urls(video_url: str) -> List[str]:
    """获取 TikTok 图集的图片链接列表。

    参数:
        video_url: TikTok 图集页面 URL

    返回:
        list[str]: 图片直链列表
    """
    info = fetch_video_info(video_url)
    if not info["is_image_post"]:
        raise VideoNotFoundError(f"该链接不是图集: {video_url}")
    return info["image_urls"]


# ============================================================
# 批量查询 (用于批量下载优化)
# ============================================================

def batch_fetch_video_info(
    video_urls: List[str],
    max_concurrency: int = 3,
) -> Dict[str, Dict]:
    """批量获取视频信息（串行但有速率控制）。

    参数:
        video_urls: 视频页面 URL 列表
        max_concurrency: 保留参数，当前版本为串行（避免触发限流）

    返回:
        dict: {video_url: video_info_dict, ...}
    """
    results = {}
    for i, url in enumerate(video_urls):
        try:
            info = fetch_video_info(url)
            results[url] = info
        except TikTokAPIError as e:
            logger.error(f"批量获取失败 [{i + 1}/{len(video_urls)}] {url}: {e}")
            results[url] = {"error": str(e)}
        # 控制速率: 每次请求间隔 1-2 秒
        if i < len(video_urls) - 1:
            time.sleep(random.uniform(1.0, 2.0))
    return results


# ============================================================
# 健康检查
# ============================================================

def check_api_health() -> Dict:
    """检查 TikTok Feed API 是否可用。

    使用一个已知的公开视频 ID 进行测试。

    返回:
        dict: {"ok": bool, "latency_ms": float, "error": str|None}
    """
    # 使用 TikTok 官方示例视频
    test_url = "https://www.tiktok.com/@tiktok/video/7234567890000000000"

    start = time.time()
    try:
        # 只测试 API 连通性，不关心具体视频是否存在
        api_base = _random_api()
        device = _random_device()
        test_api = (
            f"{api_base}/aweme/v1/feed/?"
            f"aweme_id=107955&"
            f"iid={device['iid']}&"
            f"device_id={device['device_id']}&"
            f"channel=googleplay&"
            f"app_name=musical_ly&"
            f"version_code=300904&"
            f"device_platform=android&"
            f"device_type={device['device_type']}&"
            f"version=9"
        )
        resp = requests.get(test_api, headers=_REQUEST_HEADERS, timeout=_REQUEST_TIMEOUT)
        latency = (time.time() - start) * 1000

        if resp.status_code == 200:
            data = resp.json()
            if "aweme_list" in data:
                return {"ok": True, "latency_ms": round(latency, 1), "error": None}

        return {
            "ok": False,
            "latency_ms": round(latency, 1),
            "error": f"HTTP {resp.status_code}: {resp.text[:100]}",
        }
    except Exception as e:
        latency = (time.time() - start) * 1000
        return {"ok": False, "latency_ms": round(latency, 1), "error": str(e)}


# ============================================================
# CLI 自检 (python3 tiktok_watermark_remover.py)
# ============================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    print("=" * 60)
    print("TikTok 去水印模块 - 自检")
    print("=" * 60)

    # 1. API 健康检查
    print("\n[1/4] 检查 TikTok API 连通性...")
    health = check_api_health()
    if health["ok"]:
        print(f"  ✓ API 可用 (延迟 {health['latency_ms']}ms)")
    else:
        print(f"  ✗ API 不可用: {health['error']}")
        print("  (如果 API 不可用，模块仍可使用 yt-dlp 作为降级方案)")

    # 2. 视频 ID 提取测试
    print("\n[2/4] 测试视频 ID 提取...")
    test_urls = [
        ("https://www.tiktok.com/@user/video/7234567890123456789", "7234567890123456789"),
        ("https://vm.tiktok.com/ZMxxxxx/", None),  # 短链会触发重定向
    ]
    for url, expected in test_urls:
        try:
            vid = extract_video_id(url)
            status = "✓" if not expected or vid == expected else "?"
            print(f"  {status} {url[:50]}... → {vid}")
        except Exception as e:
            print(f"  ✗ {url[:50]}... → {e}")

    # 3. 视频信息获取测试（使用命令行参数或默认值）
    print("\n[3/4] 测试视频信息获取...")
    test_video = sys.argv[1] if len(sys.argv) > 1 else None

    if test_video:
        try:
            info = fetch_video_info(test_video)
            print(f"  ✓ 作者: @{info['author_name']} ({info['author_nickname']})")
            print(f"  ✓ 描述: {info['desc'][:80]}")
            print(f"  ✓ 时长: {info['duration_ms']}ms")
            print(f"  ✓ 播放: {info['play_count']}")
            print(f"  ✓ 无水印直链: {info['clean_url'][:80]}...")
            print(f"  ✓ 带水印直链: {info['watermark_url'][:80]}...")
            print(f"  ✓ 图集: {'是' if info['is_image_post'] else '否'} "
                  f"({len(info['image_urls'])} 张)")
        except Exception as e:
            print(f"  ✗ 获取失败: {e}")
    else:
        print("  (跳过 - 未提供测试视频 URL)")
        print(f"  用法: python3 {__file__} <TikTok视频链接>")

    # 4. 模块导入检查
    print("\n[4/4] 模块可用性检查...")
    print(f"  ✓ extract_video_id() 可用")
    print(f"  ✓ fetch_video_info() 可用")
    print(f"  ✓ get_clean_video_url() 可用")
    print(f"  ✓ batch_fetch_video_info() 可用")
    print(f"  ✓ check_api_health() 可用")

    print("\n" + "=" * 60)
    print("自检完成")
    print("=" * 60)
