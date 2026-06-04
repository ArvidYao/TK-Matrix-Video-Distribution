#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Playwright 引擎（降级引擎）
==========================
将 Playwright 封装为 BrowserEngine 接口，确保在 agent-browser 不可用时自动切换。

此引擎保留完整的 Playwright 能力：
    - JS evaluate
    - 网络响应拦截
    - Cookie 注入
    - CSS 选择器查询
    - DOM 操作

用法（透明切换）：
    引擎选择由 browser_engine.BrowserEngine.create("auto") 自动处理，
    上层代码（TikTokScraper）无需感知底层引擎差异。
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional, Any, List, Dict, Callable

from browser_engine import (
    AbstractPage, AbstractSession, BrowserEngine,
    BrowserSessionContext, register_engine,
)

logger = logging.getLogger("PlaywrightEngine")

# ── 类型别名（延迟导入避免缺依赖时崩溃）──

_PW_SYNC = None  # playwright.sync_api 模块


def _get_playwright():
    """延迟导入 Playwright"""
    global _PW_SYNC
    if _PW_SYNC is None:
        try:
            from playwright.sync_api import sync_playwright
            _PW_SYNC = sync_playwright
        except ImportError:
            raise ImportError(
                "Playwright 未安装。请运行: pip install playwright && python -m playwright install chrome"
            )
    return _PW_SYNC


# ── PlaywrightPage ──

class PlaywrightPage(AbstractPage):
    """Playwright 页面适配器"""

    def __init__(self, pw_page):
        self._pw = pw_page
        self._response_handlers: List[Callable] = []

    def navigate(self, url: str, timeout: int = 45000, wait_until: str = "domcontentloaded") -> str:
        self._pw.goto(url, timeout=timeout, wait_until=wait_until)
        return self._pw.content()

    def get_content(self) -> str:
        try:
            return self._pw.content()
        except Exception as e:
            logger.warning(f"get_content 失败: {e}")
            return ""

    def get_title(self) -> str:
        try:
            return self._pw.title()
        except Exception:
            return ""

    def reload(self, timeout: int = 30000) -> str:
        self._pw.reload(timeout=timeout)
        return self._pw.content()

    def scroll(self, pixels: int = None, times: int = 1):
        for _ in range(times):
            self._pw.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
            time.sleep(0.5)

    def screenshot(self, path: str = None) -> Optional[str]:
        self._pw.screenshot(path=path or f"screenshot_{int(time.time())}.png")
        return path

    def wait_for_network_idle(self, timeout: int = 5000):
        try:
            self._pw.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    def evaluate(self, expression: str) -> Any:
        """原生 JS evaluate"""
        return self._pw.evaluate(expression)

    def query_text(self, selector_hint: str = None) -> List[str]:
        """通过 DOM 查询提取文本"""
        content = self._pw.content()
        if not content:
            return []
        # 简单文本提取（不做完整 DOM 解析）
        import re
        text = re.sub(r'<[^>]+>', '\n', content)
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if selector_hint:
            hint_lower = selector_hint.lower()
            return [line for line in lines if hint_lower in line.lower()]
        return lines

    def on_response(self, handler: Callable):
        """注册网络响应拦截器（Playwright 专有能力）"""
        def _wrapper(response):
            handler(response)
        self._pw.on("response", _wrapper)
        self._response_handlers.append(_wrapper)

    def query_selector_all(self, selector: str):
        """原生 CSS 选择器查询"""
        return self._pw.query_selector_all(selector)

    def close(self):
        try:
            self._pw.close()
        except Exception:
            pass


# ── PlaywrightSession ──

class PlaywrightSession(AbstractSession):
    """Playwright BrowserContext 适配器"""

    def __init__(self, pw_context):
        self._pw = pw_context
        self._pages: List[PlaywrightPage] = []

    def new_page(self) -> PlaywrightPage:
        pw_page = self._pw.new_page()
        page = PlaywrightPage(pw_page)
        self._pages.append(page)
        return page

    def add_cookies(self, cookies: List[dict]):
        """注入 Cookie（Playwright 专有能力）"""
        self._pw.add_cookies(cookies)

    def cookies(self) -> List[dict]:
        """获取所有 Cookie"""
        return self._pw.cookies()

    def close(self):
        for page in self._pages:
            try:
                page.close()
            except Exception:
                pass
        self._pages.clear()
        try:
            self._pw.close()
        except Exception:
            pass


# ── PlaywrightEngine ──

@register_engine("playwright")
class PlaywrightEngine(BrowserEngine):
    """
    Playwright 引擎（完整能力引擎）。

    能力矩阵：
        evaluate:           ✅ 原生支持
        network_intercept:  ✅ 原生支持
        cookies:            ✅ 原生支持

    作为 agent-browser 不可用时的自动降级引擎使用。
    """

    ENGINE_NAME = "playwright"

    def __init__(self, **kwargs):
        self._playwright = None
        self._browser = None
        self._launched = False

        # 配置
        self._proxy = kwargs.get("proxy", "")
        self._ms_token = kwargs.get("ms_token", "")
        self._viewport = kwargs.get("viewport", {"width": 390, "height": 844})
        self._user_agent = kwargs.get(
            "user_agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36",
        )
        self._locale = kwargs.get("locale", "en-US")

    @property
    def name(self) -> str:
        return self.ENGINE_NAME

    def is_available(self) -> bool:
        try:
            _get_playwright()
            return True
        except ImportError:
            return False

    # 标准 Google Chrome 路径（macOS），禁止使用 Chrome for Testing
    _CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    @property
    def _user_data_dir(self) -> str:
        """持久化用户数据目录：保存 cookies / localStorage / 登录状态"""
        _dir = os.path.join(os.path.expanduser("~"), ".tiktok_scraper_chrome_profile")
        os.makedirs(_dir, exist_ok=True)
        return _dir

    def launch(self, **kwargs):
        """启动 Playwright 浏览器（持久化上下文，保留登录状态）

        使用 launch_persistent_context 替代 launch + new_context：
        - cookies / localStorage / 登录状态自动保存到用户数据目录
        - 下次启动时自动恢复，无需重复登录
        - 使用系统标准 Google Chrome（channel="chrome"）
        """
        if self._launched:
            return

        # 兜底校验：确保系统 Chrome 存在
        if not os.path.exists(self._CHROME_PATH):
            raise FileNotFoundError(
                f"未找到标准 Google Chrome: {self._CHROME_PATH}\n"
                f"请从 https://www.google.com/chrome/ 安装标准 Google Chrome"
            )
        logger.info(f"PlaywrightEngine: 使用系统 Chrome (持久化) → {self._CHROME_PATH}")
        logger.info(f"PlaywrightEngine: 用户数据目录 → {self._user_data_dir}")

        sync_pw = _get_playwright()
        self._playwright = sync_pw().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--disable-web-security",
            "--disable-features=VizDisplayCompositor",
            "--disable-gpu",
            "--disable-features=TranslateUI",
            "--disable-ipc-flooding-protection",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-extensions-with-background-pages",
            "--disable-features=RendererCodeIntegrity",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--disable-sync",
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--force-color-profile=srgb",
            "--metrics-recording-only",
            "--no-first-run",
            "--password-store=basic",
            "--use-mock-keychain",
        ]
        if self._proxy:
            launch_args.append(f"--proxy-server={self._proxy}")

        # 使用持久化上下文 — cookies 和登录状态自动保存/恢复
        self._browser = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._user_data_dir,
            channel="chrome",
            headless=False,
            viewport=self._viewport,
            user_agent=self._user_agent,
            locale=self._locale,
            args=launch_args,
        )
        self._launched = True

        # 注入 ms_token（如果提供了）
        if self._ms_token:
            self._browser.add_cookies([{
                "name": "ms_token",
                "value": self._ms_token,
                "domain": ".tiktok.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "None",
            }])
            logger.info("PlaywrightEngine: ms_token 已注入")

        logger.info("PlaywrightEngine: 浏览器已启动（持久化上下文，登录状态将保留）")

    def session(self, **kwargs) -> BrowserSessionContext:
        """创建浏览器会话（复用持久化上下文，保留登录状态）"""
        if not self._launched:
            self.launch(**kwargs)

        # 持久化上下文已包含 viewport / UA / cookies — 直接复用
        session = PlaywrightSession(self._browser)
        return BrowserSessionContext(self, session)

    def close(self):
        """关闭浏览器（持久化上下文会自动保存 cookies 等状态到磁盘）"""
        if not self._launched:
            return
        try:
            self._browser.close()
            logger.info("PlaywrightEngine: 浏览器已关闭（登录状态已保存）")
        except Exception as e:
            logger.warning(f"PlaywrightEngine: 关闭浏览器失败: {e}")
        try:
            self._playwright.stop()
        except Exception:
            pass
        self._launched = False
        self._browser = None
        self._playwright = None

    def supports_evaluate(self) -> bool:
        return True

    def supports_network_intercept(self) -> bool:
        return True

    def supports_cookies(self) -> bool:
        return True
