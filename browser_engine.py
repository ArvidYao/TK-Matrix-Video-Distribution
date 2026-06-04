#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器引擎抽象层
================
目的：将浏览器自动化操作与具体实现解耦，支持 agent-browser CLI 和 Playwright 双引擎。

架构：
    TikTokScraper ──→ BrowserEngine (抽象接口)
                         ├── AgentBrowserEngine  (agent-browser CLI, 主引擎)
                         └── PlaywrightEngine    (Playwright, 降级引擎)

使用方式：
    # 自动选择最优引擎
    engine = BrowserEngine.create()

    # 显式指定引擎
    engine = BrowserEngine.create("agent_browser")
    engine = BrowserEngine.create("playwright")

    # 上下文管理
    with engine.session() as session:
        page = session.new_page()
        content = page.navigate("https://example.com")
        data = page.extract_text()

设计原则：
    - 引擎最低能力基线：navigate / get_content / scroll / screenshot / close
    - evaluate 为可选能力（agent-browser 不支持原生 evaluate，通过内容解析降级）
    - 网络拦截为高级能力（仅 Playwright 引擎支持，标为 optional）
"""

import abc
import logging
import time
from pathlib import Path
from typing import Optional, Callable, Any, Dict, List

logger = logging.getLogger("BrowserEngine")

# ── 引擎注册表 ──
_ENGINE_REGISTRY: Dict[str, type] = {}


def register_engine(name: str):
    """装饰器：注册引擎类到全局注册表"""
    def wrapper(cls):
        _ENGINE_REGISTRY[name] = cls
        return cls
    return wrapper


# ── 抽象基类：页面 ──

class AbstractPage(abc.ABC):
    """单个浏览器页面的抽象接口"""

    @abc.abstractmethod
    def navigate(self, url: str, timeout: int = 45000, wait_until: str = "domcontentloaded") -> str:
        """导航到 URL，返回页面内容（文本）"""
        ...

    @abc.abstractmethod
    def get_content(self) -> str:
        """获取当前页面完整 HTML 内容"""
        ...

    @abc.abstractmethod
    def get_title(self) -> str:
        """获取页面标题"""
        ...

    @abc.abstractmethod
    def reload(self, timeout: int = 30000) -> str:
        """刷新页面，返回新内容"""
        ...

    @abc.abstractmethod
    def scroll(self, pixels: int = None, times: int = 1):
        """滚动页面"""
        ...

    @abc.abstractmethod
    def screenshot(self, path: str = None) -> Optional[str]:
        """截图，返回文件路径"""
        ...

    @abc.abstractmethod
    def wait_for_network_idle(self, timeout: int = 5000):
        """等待网络空闲"""
        ...

    @abc.abstractmethod
    def evaluate(self, expression: str) -> Any:
        """
        在页面上下文中执行 JavaScript（***可选能力***）

        agent-browser 引擎通过解析页面内容降级实现简单表达式；
        复杂表达式建议使用 Playwright 引擎。

        返回值：表达式结果（或 None 表示不支持）
        """
        ...

    @abc.abstractmethod
    def query_text(self, selector_hint: str = None) -> List[str]:
        """
        从页面提取文本数据（agent-browser 原生能力）

        不依赖 CSS 选择器，而是基于 snapshot 输出进行模糊匹配。

        Args:
            selector_hint: 可选的文本模式提示（如 "video", "play", "author"）

        Returns:
            匹配的文本片段列表
        """
        ...

    @abc.abstractmethod
    def close(self):
        """关闭页面"""
        ...


# ── 抽象基类：会话（BrowserContext） ──

class AbstractSession(abc.ABC):
    """浏览器会话（对应 Playwright 的 BrowserContext）"""

    @abc.abstractmethod
    def new_page(self) -> AbstractPage:
        """创建新页面"""
        ...

    @abc.abstractmethod
    def close(self):
        """关闭会话"""
        ...


# ── 抽象基类：浏览器引擎 ──

class BrowserEngine(abc.ABC):
    """
    浏览器引擎抽象基类

    定义引擎的完整生命周期：
        1. create()  - 工厂方法，自动选择最优引擎
        2. launch()  - 启动浏览器
        3. session() - 创建浏览器会话（上下文管理器）
        4. close()   - 关闭浏览器
    """

    # ── 工厂方法 ──

    @staticmethod
    def create(engine_name: str = "auto", **kwargs) -> "BrowserEngine":
        """
        工厂方法：根据名称创建引擎实例。

        Args:
            engine_name: "auto" | "agent_browser" | "playwright"
                         "auto" 自动选择：优先 agent_browser，不可用时降级 playwright
            **kwargs: 传递给引擎构造函数的参数

        Returns:
            BrowserEngine 实例
        """
        # 确保引擎已注册
        if not _ENGINE_REGISTRY:
            BrowserEngine._auto_import_engines()

        if engine_name == "auto":
            # 自动选择：优先 agent_browser
            preferred = ["agent_browser", "playwright"]
            for name in preferred:
                if name in _ENGINE_REGISTRY:
                    try:
                        engine = _ENGINE_REGISTRY[name](**kwargs)
                        if engine.is_available():
                            logger.info(f"BrowserEngine: 自动选择 '{name}' 引擎")
                            return engine
                    except Exception as e:
                        logger.warning(f"BrowserEngine: '{name}' 引擎不可用: {e}")
            raise RuntimeError(
                "没有可用的浏览器引擎。请安装 agent-browser (npm i -g agent-browser) "
                "或 playwright (pip install playwright)。"
            )

        if engine_name in _ENGINE_REGISTRY:
            engine = _ENGINE_REGISTRY[engine_name](**kwargs)
            if not engine.is_available():
                raise RuntimeError(f"'{engine_name}' 引擎不可用，请检查安装。")
            logger.info(f"BrowserEngine: 使用 '{engine_name}' 引擎")
            return engine

        available = list(_ENGINE_REGISTRY.keys())
        raise ValueError(
            f"未知引擎 '{engine_name}'。可用引擎: {available}"
        )

    @staticmethod
    def list_engines() -> List[str]:
        """列出所有已注册引擎"""
        if not _ENGINE_REGISTRY:
            BrowserEngine._auto_import_engines()
        return list(_ENGINE_REGISTRY.keys())

    @staticmethod
    def _auto_import_engines():
        """自动导入 engines 包以触发 @register_engine 装饰器"""
        try:
            import engines  # noqa: F401
        except ImportError:
            pass

    # ── 实例方法 ──

    @abc.abstractmethod
    def is_available(self) -> bool:
        """检查引擎是否可用（CLI 已安装、依赖满足）"""
        ...

    @abc.abstractmethod
    def launch(self, **kwargs):
        """启动浏览器"""
        ...

    @abc.abstractmethod
    def session(self, **kwargs) -> "BrowserSessionContext":
        """创建浏览器会话（返回上下文管理器）"""
        ...

    @abc.abstractmethod
    def close(self):
        """关闭浏览器并释放资源"""
        ...

    @abc.abstractmethod
    def supports_evaluate(self) -> bool:
        """是否支持 JS evaluate 能力"""
        ...

    @abc.abstractmethod
    def supports_network_intercept(self) -> bool:
        """是否支持网络请求拦截"""
        ...

    @abc.abstractmethod
    def supports_cookies(self) -> bool:
        """是否支持 Cookie 注入"""
        ...

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """引擎名称"""
        ...


# ── 会话上下文管理器（统一 open/close 模式）──

class BrowserSessionContext:
    """
    浏览器会话的上下文管理器。

    用法:
        with engine.session() as s:
            page = s.new_page()
            page.navigate("https://example.com")
            content = page.get_content()
    """

    def __init__(self, engine: BrowserEngine, session: AbstractSession):
        self.engine = engine
        self._session = session

    def __enter__(self) -> AbstractSession:
        return self._session

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._session.close()
        except Exception as e:
            logger.warning(f"会话关闭异常: {e}")
        return False  # 不吞掉异常


# ── 能力查询辅助 ──

class EngineCapabilities:
    """引擎能力描述，供上层代码做分支决策"""

    def __init__(self, engine: BrowserEngine):
        self.evaluate = engine.supports_evaluate()
        self.network_intercept = engine.supports_network_intercept()
        self.cookies = engine.supports_cookies()
        self.name = engine.name

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "evaluate": self.evaluate,
            "network_intercept": self.network_intercept,
            "cookies": self.cookies,
        }
