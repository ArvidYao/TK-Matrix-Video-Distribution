#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok 热门视频爬虫模块（Web 版）
================================
整合到 TK 视频分发工具中，作为 Web 端功能。

功能：
  - 按 hashtag 搜索 TikTok 热门视频
  - 筛选播放量 >= 100k 且时长 >= 70 秒
  - 输出 CSV 文件供审核
  - 支持自定义 hashtag、筛选条件

调用方式（被 web_app.py 调用）：
  from tiktok_scraper import TikTokScraper
  scraper = TikTokScraper(
      ms_token="xxx",
      proxy="http://127.0.0.1:7890",
      hashtags=["Euphoria", "HouseOfTheDragon"],
      min_plays=100000,
      min_duration=70,
      max_scrolls=5,
      on_progress=callback_func,
  )
  results = scraper.run()

引擎架构（v2.0）：
  使用 browser_engine 抽象层替代直接 Playwright 调用：
      TikTokScraper ──→ BrowserEngine (抽象接口)
                           ├── AgentBrowserEngine  (agent-browser CLI, 主引擎)
                           └── PlaywrightEngine    (Playwright, 降级引擎)

  默认自动选择 agent-browser，不可用时降级为 Playwright。
"""

import csv
import json
import os
import re
import subprocess
import time
import logging
from datetime import datetime
from pathlib import Path

# 浏览器引擎抽象层
from browser_engine import BrowserEngine, EngineCapabilities

logger = logging.getLogger("TKScraper")

# ---- 浏览器强制校验：只允许标准 Google Chrome，禁止 Chrome for Testing / Chromium ----

# 标准 Google Chrome 可执行文件路径（macOS）
_CHROME_EXECUTABLE_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 禁止使用的浏览器路径关键字
_FORBIDDEN_BROWSER_KEYWORDS = [
    "for testing",
    "for-testing",
    "chromium",
]


def _validate_chrome_executable(exe_path):
    """强制校验浏览器可执行文件是否为标准 Google Chrome。

    验证流程：
    1. 路径存在性检查
    2. 路径关键字黑名单过滤（阻断 Chrome for Testing / Chromium）
    3. 调用 --version 验证版本字符串必须包含 "Google Chrome"

    Raises:
        FileNotFoundError: 可执行文件不存在
        AssertionError:  浏览器不是标准 Google Chrome
    """
    if not exe_path or not os.path.exists(exe_path):
        raise FileNotFoundError(
            f"Chrome 可执行文件不存在: {exe_path}\n"
            f"请安装标准 Google Chrome 或设置环境变量 CHROME_EXECUTABLE_PATH"
        )

    lower_path = exe_path.lower()
    for kw in _FORBIDDEN_BROWSER_KEYWORDS:
        if kw in lower_path:
            raise AssertionError(
                f"！！浏览器配置错误：检测到禁止的浏览器变体！！\n"
                f"当前路径: {exe_path}\n"
                f"触发关键字: '{kw}'\n"
                f"请使用标准 Google Chrome: {_CHROME_EXECUTABLE_PATH}\n"
                f"或设置环境变量 CHROME_EXECUTABLE_PATH 指向标准 Chrome。"
            )

    # 通过 --version 二次验证
    try:
        result = subprocess.run(
            [exe_path, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        version_str = (result.stdout.strip() + result.stderr.strip()).strip()
    except subprocess.TimeoutExpired:
        logger.warning("Chrome --version 调用超时，跳过版本校验")
        return exe_path
    except Exception as e:
        logger.warning(f"Chrome --version 调用异常: {e}，跳过版本校验")
        return exe_path

    if not version_str:
        logger.warning("Chrome --version 返回空，跳过版本校验")
        return exe_path

    # 黑名单：版本字符串不得包含禁止关键字
    lower_version = version_str.lower()
    for kw in _FORBIDDEN_BROWSER_KEYWORDS:
        if kw in lower_version:
            raise AssertionError(
                f"！！浏览器版本校验失败：检测到禁止的浏览器变体！！\n"
                f"路径: {exe_path}\n"
                f"版本: {version_str}\n"
                f"触发关键字: '{kw}'\n"
                f"请使用标准 Google Chrome: {_CHROME_EXECUTABLE_PATH}\n"
                f"或设置环境变量 CHROME_EXECUTABLE_PATH 指向标准 Chrome。"
            )

    # 白名单：必须包含 "Google Chrome"
    if "Google Chrome" not in version_str:
        raise AssertionError(
            f"！！浏览器版本校验失败：不是标准 Google Chrome！！\n"
            f"路径: {exe_path}\n"
            f"版本: {version_str}\n"
            f"请使用标准 Google Chrome: {_CHROME_EXECUTABLE_PATH}\n"
            f"或设置环境变量 CHROME_EXECUTABLE_PATH 指向标准 Chrome。"
        )

    logger.info(f"Chrome 浏览器校验通过: {version_str}")
    return exe_path

# 默认 hashtag 列表（美剧/英剧为主）
DEFAULT_HASHTAGS = [
    "Euphoria",
    "HouseOfTheDragon",
    "WednesdayAddams",
    "StrangerThings",
    "SquidGame",
    "Severance",
    "TheBear",
    "WhiteLotus",
    "Bridgerton",
    "filmtok",
]


def format_number(num):
    if num is None:
        return "N/A"
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)


def format_duration(seconds):
    if seconds is None:
        return "N/A"
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{mins}:{secs:02d}"


def parse_duration_from_video_item(item):
    """从 TikTok 视频条目中智能提取时长（秒）

    TikTok API 中 video.duration 字段可能是秒或毫秒，取决于 API 版本。
    此函数自动检测并转换为统一的秒数。

    Args:
        item: TikTok 视频条目 dict

    Returns:
        float: 时长（秒），解析失败返回 0
    """
    video_info = item.get("video", {}) if isinstance(item, dict) else {}

    # 主路径：video.duration
    raw_duration = video_info.get("duration", 0) or 0

    # 次路径：music.duration（部分 API 版本中音频时长匹配视频时长）
    if not raw_duration:
        music_info = item.get("music", {}) or {}
        raw_duration = music_info.get("duration", 0) or 0

    # 次路径：video.infos 嵌套
    if not raw_duration:
        infos = video_info.get("infos", {}) or {}
        raw_duration = infos.get("duration", 0) or 0

    if not raw_duration or raw_duration <= 0:
        return 0.0

    raw_duration = float(raw_duration)

    # 智能检测单位：
    # - TikTok 视频最大 60 分钟（3600 秒）
    # - 大于 10000 的值必然是毫秒（10000ms = 10秒，TikTok 最短视频约 1-3 秒）
    # - 使用更保守的阈值 36000（10小时），避免误判长视频
    MAX_REASONABLE_SECONDS = 36000  # 10 小时，远超 TikTok 限制

    if raw_duration > MAX_REASONABLE_SECONDS:
        # 毫秒 → 秒
        logger.debug(f"  时长: {raw_duration}ms → {raw_duration / 1000:.1f}s (毫秒转秒)")
        return raw_duration / 1000.0
    else:
        # 已经是秒
        logger.debug(f"  时长: {raw_duration}s")
        return raw_duration


def parse_play_count_text(text):
    """解析 '1.2M', '500K', '12345' 格式"""
    if not text:
        return 0
    text = str(text).strip().upper().replace(",", "")
    text = re.sub(r'[^\dMK]', '', text)
    try:
        if "M" in text:
            return int(float(text.replace("M", "")) * 1_000_000)
        elif "K" in text:
            return int(float(text.replace("K", "")) * 1_000)
        else:
            return int(text)
    except (ValueError, IndexError):
        return 0


def parse_duration_text(text):
    """解析 '1:30', '2:05' 格式"""
    if not text:
        return 0
    parts = str(text).strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        pass
    return 0


def extract_from_api_response(response_body, hashtag_name):
    """从 TikTok 内部 API 的 JSON 响应中提取视频数据"""
    videos = []
    try:
        data = json.loads(response_body)
    except (json.JSONDecodeError, TypeError):
        return videos

    item_list = None

    if isinstance(data, dict):
        if "itemList" in data:
            item_list = data["itemList"]
        elif "data" in data and isinstance(data["data"], dict):
            item_list = data["data"].get("itemList", [])
        elif "body" in data and isinstance(data["body"], dict):
            if "itemList" in data["body"]:
                item_list = data["body"]["itemList"]
            elif "data" in data["body"] and isinstance(data["body"]["data"], dict):
                item_list = data["body"]["data"].get("itemList", [])
        elif "itemModule" in data:
            item_list = list(data["itemModule"].values()) if isinstance(data["itemModule"], dict) else []

    if not item_list:
        return videos

    for item in item_list:
        if not isinstance(item, dict):
            continue
        try:
            item_id = str(item.get("id", ""))
            if not item_id:
                continue

            stats = item.get("stats", {})
            if not stats:
                stats = item.get("author", {}).get("stats", {})
            plays = stats.get("playCount", 0) or 0
            if isinstance(plays, str):
                plays = parse_play_count_text(plays)

            video_info = item.get("video", {})
            duration_sec = parse_duration_from_video_item(item)

            author_info = item.get("author", {})
            author_name = ""
            if isinstance(author_info, dict):
                author_name = (author_info.get("uniqueId", "")
                               or author_info.get("nickname", "")
                               or author_info.get("id", ""))

            desc = item.get("desc", "")[:200]

            create_time = item.get("createTime", 0)
            create_date = ""
            if create_time:
                try:
                    create_date = datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    pass

            if author_name and item_id:
                video_url = f"https://www.tiktok.com/@{author_name}/video/{item_id}"
            else:
                video_url = f"https://www.tiktok.com/video/{item_id}"

            videos.append({
                "video_id": item_id,
                "video_url": video_url,
                "author": author_name,
                "description": desc,
                "plays": int(plays),
                "likes": int(stats.get("diggCount", 0) or 0),
                "comments": int(stats.get("commentCount", 0) or 0),
                "shares": int(stats.get("shareCount", 0) or 0),
                "duration_sec": duration_sec,
                "duration_display": format_duration(duration_sec),
                "create_time": create_date,
            })
        except Exception:
            continue

    return videos


def extract_from_sigi_state(page, hashtag_name):
    """从页面的 __SIGI_STATE__ 或 __NEXT_DATA__ 提取视频（增强版）

    支持多种 TikTok 页面状态数据结构，递归搜索 itemModule。
    """
    videos = []

    for js_var in ["__SIGI_STATE__", "__NEXT_DATA__"]:
        try:
            state = page.evaluate(f"() => window.{js_var}")
        except Exception:
            continue

        if not state or not isinstance(state, dict):
            continue

        # 递归搜索 itemModule
        item_module = _find_item_module(state)
        if not item_module:
            logger.debug(f"  SIGI: {js_var} 中未找到 itemModule")
            continue

        logger.info(f"  SIGI: {js_var} 找到 itemModule，包含 {len(item_module)} 个条目")
        for item_id, item in item_module.items():
            if not isinstance(item, dict):
                continue
            try:
                v = _parse_sigi_item(item, item_id)
                if v:
                    videos.append(v)
                    v["hashtag"] = hashtag_name
            except Exception:
                continue

    return videos


def _find_item_module(obj, max_depth=10):
    """递归搜索数据结构中的 itemModule"""
    if max_depth <= 0:
        return None
    if isinstance(obj, dict):
        if "itemModule" in obj and isinstance(obj["itemModule"], dict):
            return obj["itemModule"]
        # 递归搜索子节点
        for key in obj:
            result = _find_item_module(obj[key], max_depth - 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_item_module(item, max_depth - 1)
            if result:
                return result
    return None


def _parse_sigi_item(item, item_id):
    """解析单个 SIGI_STATE 中的视频条目"""
    stats = item.get("stats", {})
    if not stats:
        stats = item.get("author", {}).get("stats", {})

    plays = stats.get("playCount", 0) or 0
    if isinstance(plays, str):
        plays = parse_play_count_text(plays)

    video_info = item.get("video", {})
    duration_sec = parse_duration_from_video_item(item)

    author_info = item.get("author", {})
    author_name = ""
    if isinstance(author_info, dict):
        author_name = (author_info.get("uniqueId", "")
                       or author_info.get("nickname", ""))

    desc = item.get("desc", "")[:200]
    create_time = item.get("createTime", 0)
    create_date = ""
    if create_time:
        try:
            create_date = datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    video_url = f"https://www.tiktok.com/@{author_name}/video/{item_id}" if author_name else ""

    return {
        "video_id": item_id,
        "video_url": video_url,
        "author": author_name,
        "description": desc,
        "plays": int(plays),
        "likes": int(stats.get("diggCount", 0) or 0),
        "comments": int(stats.get("commentCount", 0) or 0),
        "shares": int(stats.get("shareCount", 0) or 0),
        "duration_sec": duration_sec,
        "duration_display": format_duration(duration_sec),
        "create_time": create_date,
    }


def extract_from_dom(page):
    """兜底：从页面内容文本中提取视频信息（引擎无关，兼容 agent-browser 和 Playwright）

    策略：
        1. 获取页面 HTML 源码 + 文本内容
        2. 从内容中提取所有 /video/xxx 链接
        3. 从相邻文本区域推测播放量、时长等信息
        4. 尝试从 HTML 中提取内嵌 JSON 数据（如 __UNIVERSAL_DATA_FOR_REHYDRATION__）
    """
    videos = []
    try:
        content = page.get_content()
    except Exception:
        return videos

    if not content:
        return videos

    # 额外尝试：从 HTML 源码中提取 TikTok 内嵌数据
    try:
        html = page.evaluate("() => document.documentElement.outerHTML")
        if html:
            # 尝试提取 __UNIVERSAL_DATA_FOR_REHYDRATION__
            jsonv_match = re.search(
                r'<script[^>]*id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>\s*(\{.*?\})\s*</script>',
                html, re.DOTALL
            )
            if jsonv_match:
                try:
                    u_data = json.loads(jsonv_match.group(1))
                    item_module = _find_item_module(u_data)
                    if item_module:
                        logger.info(f"  [DOM-UniversalData] 找到 {len(item_module)} 个视频条目")
                        for item_id, item in item_module.items():
                            if isinstance(item, dict):
                                v = _parse_sigi_item(item, item_id)
                                if v and v.get("video_id"):
                                    videos.append(v)
                        if videos:
                            return videos
                except (json.JSONDecodeError, Exception):
                    pass
    except Exception:
        pass

    # 提取所有 /video/ID 或 /@user/video/ID 模式的链接
    video_url_pattern = re.compile(
        r'(?:https?://(?:www\.)?tiktok\.com)?(/@[\w.-]+/video/(\d+)|/video/(\d+))',
        re.IGNORECASE
    )
    seen_ids = set()

    for match in video_url_pattern.finditer(content):
        video_id = match.group(2) or match.group(3)
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)

        # 提取视频 URL 周围的文本上下文（前后各 500 字符）
        pos = match.start()
        context_start = max(0, pos - 500)
        context_end = min(len(content), pos + 500)
        context = content[context_start:context_end]

        # 从上下文中提取作者名
        author = ""
        author_match = re.search(r'/@([\w.-]+)/video/', match.group(0))
        if author_match:
            author = author_match.group(1)

        # 从上下文中提取播放量（匹配 1.2M / 500K / 12345 等模式）
        plays = 0
        play_patterns = [
            r'(\d+\.?\d*)\s*[MK]\s*(?:plays|views|播放)',
            r'(?:plays|views|播放)[:\s]*(\d+\.?\d*)\s*[MK]?',
            r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*[MK]?\s*(?:plays|views)',
        ]
        for pp in play_patterns:
            pm = re.search(pp, context, re.IGNORECASE)
            if pm:
                plays = parse_play_count_text(pm.group(1))
                if plays > 0:
                    break

        # 从上下文中提取时长
        duration_sec = 0
        dur_patterns = [
            # MM:SS 或 H:MM:SS 格式（最常见）
            r'(\d{1,2}:\d{2}(?::\d{2})?)',
            # "duration: 1:30" 格式
            r'duration[:\s]*(\d{1,2}:\d{2})',
            # "1 min 30 sec" 格式
            r'(\d+)\s*min\s*(\d+)\s*sec',
            # "45 sec" 格式
            r'(\d+)\s*sec',
            # data-duration HTML 属性
            r'data-duration=["\'](\d+)["\']',
            # aria-label 中包含时长
            r'aria-label=["\'][^"\']*?(\d{1,2}:\d{2})[^"\']*?["\']',
        ]
        dur_text = ""
        for dp in dur_patterns:
            dm = re.search(dp, context, re.IGNORECASE)
            if dm:
                if len(dm.groups()) == 2 and dm.group(2):  # "min sec" 格式
                    duration_sec = int(dm.group(1)) * 60 + int(dm.group(2))
                    dur_text = f"{dm.group(1)}:{dm.group(2).zfill(2)}"
                elif "data-duration" in dp:
                    # data-duration 可能是毫秒
                    raw = int(dm.group(1))
                    duration_sec = raw / 1000.0 if raw > 10000 else float(raw)
                else:
                    dur_text = dm.group(1)
                    duration_sec = parse_duration_text(dur_text)
                if duration_sec > 0:
                    break

        # 从上下文中提取描述（取链接前后较长的非 URL 文本）
        desc = ""
        desc_match = re.search(r'(?:desc|title|caption)[:\s]*([^\n]{10,200})', context, re.IGNORECASE)
        if desc_match:
            desc = desc_match.group(1).strip()
        else:
            # 取链接后第一段非 URL 文本
            after_link = content[pos + len(match.group(0)):pos + len(match.group(0)) + 300]
            text_parts = re.findall(r'[A-Za-z\u4e00-\u9fff][^\n]{10,200}', after_link)
            if text_parts:
                desc = text_parts[0].strip()

        if author and "@" not in author:
            video_url = f"https://www.tiktok.com/@{author}/video/{video_id}"
        else:
            video_url = f"https://www.tiktok.com/video/{video_id}"

        videos.append({
            "video_id": video_id,
            "video_url": video_url,
            "author": author.replace("@", ""),
            "description": desc[:200] if desc else "",
            "plays": plays,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "duration_sec": duration_sec,
            "duration_display": dur_text or format_duration(duration_sec),
            "create_time": "",
        })

    return videos


class TikTokScraper:
    """TikTok 热门视频爬虫（v2.0 - 引擎抽象层）

    支持进度回调，适配 Web 端 SSE 推送。
    使用 browser_engine 抽象层，支持 agent-browser CLI 和 Playwright 双引擎。
    """

    def __init__(self, ms_token="", proxy="", hashtags=None,
                 min_plays=100_000, min_duration=70, max_scrolls=5,
                 on_progress=None, should_stop_fn=None,
                 engine_name="auto"):
        """
        Args:
            ms_token: TikTok cookie ms_token
            proxy: HTTP 代理地址（如 http://127.0.0.1:7890）
            hashtags: 要搜索的 hashtag 列表
            min_plays: 最低播放量筛选
            min_duration: 最低时长筛选（秒）
            max_scrolls: 每个 hashtag 滚动次数
            on_progress: 进度回调函数 fn(event_type, data)
            should_stop_fn: 检查是否需要停止 fn() -> bool
            engine_name: 浏览器引擎名称 ("auto"|"agent_browser"|"playwright")
        """
        self.ms_token = ms_token
        self.proxy = proxy
        self.hashtags = hashtags or DEFAULT_HASHTAGS
        self.min_plays = min_plays
        self.min_duration = min_duration
        self.max_scrolls = max_scrolls
        self.on_progress = on_progress or (lambda *a, **k: None)
        self.should_stop_fn = should_stop_fn or (lambda: False)
        self.results = []
        self.qualified = []
        self.is_running = False
        self.output_dir = Path(__file__).parent / "logs"
        self.engine_name = engine_name
        self._capabilities = None  # 运行时确定的引擎能力

    def _emit(self, event_type, data=None):
        """发送进度事件"""
        msg = {"type": event_type, "timestamp": datetime.now().isoformat()}
        if data:
            msg.update(data)
        try:
            self.on_progress(event_type, msg)
        except Exception:
            pass

    def _scrape_one_hashtag(self, session, hashtag_name, has_network_intercept):
        """爬取单个 hashtag 的视频

        Args:
            session: 浏览器会话（AbstractSession）
            hashtag_name: hashtag 名称
            has_network_intercept: 引擎是否支持网络拦截
        """
        results = []
        logger.info(f"正在访问 #{hashtag_name}")
        self._emit("hashtag_start", {
            "hashtag": hashtag_name,
            "message": f"正在访问 #{hashtag_name}...",
        })

        page = None
        try:
            page = session.new_page()
            captured_videos = []

            # ── Layer 1: API 响应拦截（仅 Playwright 引擎支持）──
            if has_network_intercept:
                # 通过 engine 透传的 Playwright page 对象注册拦截器
                pw_page = getattr(page, '_pw', None)
                if pw_page:

                    def handle_response(response):
                        if self.should_stop_fn():
                            return
                        url = response.url
                        if any(kw in url for kw in [
                            "challenge/item_list", "feed/", "recommend/item_list",
                            "search/item_list", "tag/", "challenge/",
                        ]):
                            try:
                                if response.status == 200:
                                    body = response.text()
                                    if body and len(body) > 100:
                                        extracted = extract_from_api_response(body, hashtag_name)
                                        captured_videos.extend(extracted)
                                        if extracted:
                                            logger.info(f"  [API拦截] #{hashtag_name} 获取 {len(extracted)} 条")
                            except Exception:
                                pass

                    pw_page.on("response", handle_response)

            # 导航到 hashtag 页面
            url = f"https://www.tiktok.com/tag/{hashtag_name}"
            logger.info(f"  导航到: {url}")
            page.navigate(url, timeout=45000, wait_until="domcontentloaded")
            time.sleep(3)

            # 等待页面动态内容加载完成
            try:
                page.wait_for_network_idle(timeout=8000)
            except Exception:
                logger.debug("  network_idle 超时，继续执行")
            time.sleep(3)

            title = page.get_title()
            logger.info(f"  页面标题: {title}")

            page_content = page.get_content()
            content_len = len(page_content) if page_content else 0
            logger.info(f"  页面内容长度: {content_len} 字符")

            # 截图保存以便排查（保存到 logs 目录）
            try:
                screenshot_path = str(self.output_dir / f"debug_{hashtag_name}_{datetime.now().strftime('%H%M%S')}.png")
                page.screenshot(screenshot_path)
                logger.info(f"  调试截图已保存: {screenshot_path}")
            except Exception as se:
                logger.warning(f"  截图失败: {se}")

            if "Please wait" in page_content:
                logger.info("  WAF 验证中，等待 15 秒...")
                self._emit("waf_wait", {
                    "hashtag": hashtag_name,
                    "message": f"#{hashtag_name} WAF 验证中，等待...",
                })
                time.sleep(15)
                page.reload(timeout=30000)
                time.sleep(4)

            # 滚动加载
            for scroll_i in range(self.max_scrolls):
                if self.should_stop_fn():
                    break
                page.scroll(times=1)
                time.sleep(2.5)

                try:
                    page.wait_for_network_idle(timeout=5000)
                except Exception:
                    pass
                time.sleep(1)

                self._emit("hashtag_scroll", {
                    "hashtag": hashtag_name,
                    "scroll": scroll_i + 1,
                    "max_scrolls": self.max_scrolls,
                    "captured": len(captured_videos),
                    "message": f"#{hashtag_name} 滚动 {scroll_i+1}/{self.max_scrolls}，已捕获 {len(captured_videos)} 条",
                })

            # ── Layer 2: SIGI_STATE 提取 ──
            if not captured_videos:
                extracted = extract_from_sigi_state(page, hashtag_name)
                captured_videos = extracted
                logger.info(f"  [SIGI] #{hashtag_name} 获取 {len(captured_videos)} 条")

            # ── Layer 3: DOM/文本提取（兼容 agent-browser）──
            if not captured_videos:
                captured_videos = extract_from_dom(page)
                logger.info(f"  [文本提取] #{hashtag_name} 获取 {len(captured_videos)} 条")

            # 去重
            seen = set()
            for v in captured_videos:
                vid = v.get("video_id", "")
                if vid and vid not in seen:
                    seen.add(vid)
                    results.append(v)

            # 统计时长分布（用于调试）
            durations = [v.get("duration_sec", 0) for v in results]
            non_zero = [d for d in durations if d > 0]
            if non_zero:
                logger.info(
                    f"  #{hashtag_name} 共捕获 {len(results)} 条视频 | "
                    f"时长统计: 有效 {len(non_zero)}/{len(results)} 条 | "
                    f"范围 {min(non_zero):.0f}s~{max(non_zero):.0f}s"
                )
            else:
                logger.warning(
                    f"  #{hashtag_name} 共捕获 {len(results)} 条视频 | "
                    f"⚠️ 所有时长均为 0"
                )

            self._emit("hashtag_done", {
                "hashtag": hashtag_name,
                "count": len(results),
                "message": f"#{hashtag_name} 完成，获取 {len(results)} 条视频",
            })

        except Exception as e:
            logger.error(f"访问 #{hashtag_name} 失败: {e}")
            self._emit("hashtag_error", {
                "hashtag": hashtag_name,
                "error": str(e),
                "message": f"#{hashtag_name} 访问失败: {e}",
            })
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

        return results

    def run(self):
        """执行爬取任务，返回 (all_results, qualified_results)

        使用 browser_engine 抽象层，自动选择最优引擎（agent-browser 优先）。
        """
        self.is_running = True
        self.results = []
        self.qualified = []
        start_time = time.time()

        # ── 创建浏览器引擎 ──
        try:
            engine = BrowserEngine.create(
                self.engine_name,
                proxy=self.proxy,
                ms_token=self.ms_token,
            )
        except RuntimeError as e:
            self._emit("error", {"message": str(e)})
            self.is_running = False
            return [], []

        # 记录引擎能力
        self._capabilities = EngineCapabilities(engine)
        logger.info(f"浏览器引擎: {self._capabilities.as_dict()}")

        self._emit("start", {
            "hashtags": self.hashtags,
            "min_plays": self.min_plays,
            "min_duration": self.min_duration,
            "engine": self._capabilities.as_dict(),
            "message": f"开始爬取 {len(self.hashtags)} 个 hashtag...",
        })

        try:
            engine.launch()

            self._emit("browser_launch", {
                "message": f"启动浏览器 (引擎: {engine.name})...",
            })

            with engine.session() as session:
                # ── 检测引擎能力 ──
                has_network_intercept = engine.supports_network_intercept()
                has_cookies = engine.supports_cookies()

                # ── Cookie 注入（仅支持 Cookie 的引擎）──
                if has_cookies and self.ms_token:
                    try:
                        pw_session = getattr(session, '_pw', None)
                        if pw_session:
                            pw_session.add_cookies([{
                                "name": "ms_token",
                                "value": self.ms_token,
                                "domain": ".tiktok.com",
                                "path": "/",
                                "httpOnly": False,
                                "secure": True,
                                "sameSite": "None",
                            }])
                            logger.info("ms_token 已注入浏览器")
                    except Exception as e:
                        logger.warning(f"ms_token 注入失败: {e}")

                # ── 访问首页建立 session ──
                home = None
                try:
                    home = session.new_page()
                    self._emit("home_loading", {"message": "访问 TikTok 首页建立 session..."})
                    home.navigate("https://www.tiktok.com", timeout=45000, wait_until="domcontentloaded")
                    time.sleep(5)

                    # 等待页面完全加载
                    try:
                        home.wait_for_network_idle(timeout=8000)
                    except Exception:
                        pass
                    time.sleep(2)

                    content = home.get_content()
                    content_len = len(content) if content else 0
                    logger.info(f"首页内容长度: {content_len} 字符")
                    if "Please wait" in content:
                        logger.info("首页 WAF 验证中，等待 15 秒...")
                        time.sleep(15)
                        home.reload(timeout=30000)
                        time.sleep(5)

                    title = home.get_title()
                    logger.info(f"首页标题: {title}")
                    logger.info(f"首页内容前200字: {content[:200] if content else '(空)'}")

                    if "login" in content.lower() or "Log in" in content or "sign up" in content.lower():
                        self._emit("login_required", {
                            "message": "需要登录 TikTok！请在弹出的浏览器中手动登录，登录后任务自动继续...",
                        })
                        # 等待登录（最多等 120 秒），放宽检测条件
                        for wait_i in range(24):
                            if self.should_stop_fn():
                                break
                            time.sleep(5)
                            home.reload(timeout=30000)
                            time.sleep(3)
                            new_content = home.get_content()
                            # 放宽检测：只要不包含 login/log in/sign up 就认为已登录
                            if ("login" not in new_content.lower()
                                    and "log in" not in new_content.lower()
                                    and "sign up" not in new_content.lower()):
                                logger.info("检测到登录成功")
                                break
                            if wait_i % 6 == 5:
                                logger.info(f"  仍在等待登录... ({wait_i+1}/24)")

                    logger.info("首页 session 建立完成")
                except Exception as e:
                    logger.warning(f"首页访问异常: {e}")
                finally:
                    if home:
                        try:
                            home.close()
                        except Exception:
                            pass

                # ── 逐个爬取 hashtag ──
                for i, hashtag_name in enumerate(self.hashtags):
                    if self.should_stop_fn():
                        self._emit("stopped", {"message": "任务已手动停止"})
                        break

                    self._emit("progress", {
                        "current": i + 1,
                        "total": len(self.hashtags),
                        "hashtag": hashtag_name,
                    })

                    results = self._scrape_one_hashtag(session, hashtag_name, has_network_intercept)
                    for r in results:
                        r["hashtag"] = hashtag_name
                    self.results.extend(results)

                    if i < len(self.hashtags) - 1 and not self.should_stop_fn():
                        time.sleep(3)

            # 关闭引擎
            engine.close()
            self._emit("browser_close", {"message": "浏览器已关闭"})

        except Exception as e:
            logger.error(f"爬取异常: {e}", exc_info=True)
            self._emit("error", {"message": f"爬取异常: {e}"})
            try:
                engine.close()
            except Exception:
                pass

        # 处理结果
        elapsed = time.time() - start_time
        total = len(self.results)
        self.qualified = [
            r for r in self.results
            if r["plays"] >= self.min_plays and r["duration_sec"] >= self.min_duration
        ]
        self.qualified.sort(key=lambda x: x["plays"], reverse=True)
        self.results.sort(key=lambda x: x["plays"], reverse=True)

        # 保存 CSV
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if self.results:
            all_output = self.output_dir / f"tiktok_all_{timestamp}.csv"
            fieldnames = [
                "hashtag", "video_url", "video_id", "author", "description",
                "plays", "likes", "comments", "shares",
                "duration_sec", "duration_display", "create_time",
            ]
            with open(all_output, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(self.results)

        qualified_output = None
        if self.qualified:
            qualified_output = self.output_dir / f"tiktok_hot_{timestamp}.csv"
            fieldnames = [
                "hashtag", "video_url", "video_id", "author", "description",
                "plays", "likes", "comments", "shares",
                "duration_sec", "duration_display", "create_time",
            ]
            with open(qualified_output, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(self.qualified)

        # 完成事件由 web_app.py 的 _run() 统一发送（确保结果已保存到 scraper_state）
        return self.results, self.qualified
