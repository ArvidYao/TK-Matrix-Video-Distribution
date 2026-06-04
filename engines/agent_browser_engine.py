#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Browser 引擎
==================
将 agent-browser CLI 封装为 BrowserEngine 接口。

CLI 命令映射：
    open <url>            → navigate()
    wait --load <mode>    → wait_for_network_idle()
    snapshot              → get_content()
    screenshot            → screenshot()
    close                 → 会话结束时调用

限制与降级策略：
    - 不支持 page.evaluate() JS 执行 → 通过解析 snapshot 输出降级
    - 不支持网络响应拦截 → 上层代码需要改用页面内容解析策略
    - 不支持 query_selector_all → 通过 snapshot 文本解析替代

依赖：
    npm install -g agent-browser
    agent-browser install    (首次安装 Chromium)
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Any, List, Dict

from browser_engine import (
    AbstractPage, AbstractSession, BrowserEngine,
    BrowserSessionContext, register_engine,
)

logger = logging.getLogger("AgentBrowserEngine")

# ── CLI 工具路径 ──

_AGENT_BROWSER_BIN = shutil.which("agent-browser") or "agent-browser"


def _check_cli() -> bool:
    """检查 agent-browser CLI 是否可用"""
    try:
        result = subprocess.run(
            [_AGENT_BROWSER_BIN, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run_cli(args: List[str], timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    """
    执行 agent-browser CLI 命令。

    Args:
        args: 命令参数列表（不含 'agent-browser' 前缀）
        timeout: 超时秒数
        check: 是否在失败时抛出异常

    Returns:
        subprocess.CompletedProcess
    """
    cmd = [_AGENT_BROWSER_BIN] + args
    logger.debug(f"agent-browser: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        error_msg = stderr or stdout or f"exit code {result.returncode}"
        raise RuntimeError(f"agent-browser 命令失败: {' '.join(cmd)}\n{error_msg}")
    return result


# ── AgentBrowserPage ──

class AgentBrowserPage(AbstractPage):
    """
    基于 agent-browser CLI 的页面实现。

    每个 page 实例对应 agent-browser 中的一个浏览器标签页。
    agent-browser 是单标签页模型，所以每个页面就是整个浏览器实例。
    """

    def __init__(self, session: "AgentBrowserSession"):
        self._session = session
        self._current_url = ""
        self._last_snapshot = ""
        self._closed = False

    def navigate(self, url: str, timeout: int = 45000, wait_until: str = "domcontentloaded") -> str:
        self._check_closed()
        self._current_url = url

        # 打开 URL（agent-browser 的 open 命令）
        _run_cli(["open", url], timeout=min(timeout // 1000 + 10, 60))

        # 等待页面加载
        if wait_until == "networkidle":
            self.wait_for_network_idle(timeout=min(timeout // 1000, 15))
        else:
            # domcontentloaded: 等待基本加载
            try:
                _run_cli(["wait", "--load", "load"], timeout=15)
            except Exception:
                logger.debug("wait --load load 超时，继续执行")

        # 获取页面内容
        try:
            self._last_snapshot = self._get_snapshot()
        except Exception:
            self._last_snapshot = ""
        return self._last_snapshot

    def get_content(self) -> str:
        self._check_closed()
        try:
            self._last_snapshot = self._get_snapshot()
        except Exception as e:
            logger.warning(f"get_content 失败: {e}")
        return self._last_snapshot

    def get_title(self) -> str:
        self._check_closed()
        content = self.get_content()
        # 从 snapshot 输出尝试提取 title
        # agent-browser snapshot 输出通常包含页面标题
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("http") and len(line) < 200:
                # 简单启发式：取第一行非 URL 的短文本作为标题
                return line
        return ""

    def reload(self, timeout: int = 30000) -> str:
        self._check_closed()
        if self._current_url:
            return self.navigate(self._current_url, timeout=timeout)
        return self.get_content()

    def scroll(self, pixels: int = None, times: int = 1):
        """
        滚动页面。

        agent-browser 没有直接的 scroll 命令，
        通过注入键盘事件或使用 type 命令模拟。
        这里使用多次 PageDown 键盘操作模拟滚动。
        """
        self._check_closed()
        for _ in range(times):
            try:
                # 使用 page-down 滚动
                _run_cli(["press", "PageDown"], timeout=5, check=False)
                time.sleep(1)
            except Exception:
                logger.debug("scroll PageDown 失败")

    def screenshot(self, path: str = None) -> Optional[str]:
        self._check_closed()
        if path is None:
            # 默认保存到临时目录
            tmpdir = tempfile.mkdtemp(prefix="tk_scraper_")
            path = os.path.join(tmpdir, f"screenshot_{int(time.time())}.png")

        try:
            _run_cli(["screenshot", path], timeout=15)
            if os.path.exists(path):
                return path
        except Exception as e:
            logger.warning(f"screenshot 失败: {e}")
        return None

    def wait_for_network_idle(self, timeout: int = 5000):
        """等待网络空闲"""
        self._check_closed()
        try:
            _run_cli(["wait", "--load", "networkidle"], timeout=timeout // 1000 + 5)
        except Exception:
            # networkidle 在 SPA 页面可能永不触发，降级为 load
            try:
                _run_cli(["wait", "--load", "load"], timeout=10)
            except Exception:
                logger.debug("wait --load load 超时")
                time.sleep(2)

    def evaluate(self, expression: str) -> Any:
        """
        执行 JavaScript（降级实现）。

        agent-browser 不支持原生 JS evaluate，
        这里通过解析 snapshot 内容进行有限的支持。

        支持的表达式模式：
            - () => window.__SIGI_STATE__   → 从 snapshot 提取 JSON
            - () => window.__NEXT_DATA__    → 从 snapshot 提取 JSON
            - window.__SIGI_STATE__         → 同上
            - window.scrollBy(x, y)         → 调用 scroll()
            - document.title                → 调用 get_title()
        """
        self._check_closed()

        # 从表达式（包括箭头函数）中提取 window 变量名
        window_var_match = re.search(r'window\.(\w+)', expression)

        # 匹配 window.scrollBy
        scroll_match = re.search(r"window\.scrollBy\(\s*(\d+)\s*,\s*(\d+)\s*\)", expression)
        if scroll_match:
            y = int(scroll_match.group(2))
            times = max(1, y // 500)
            self.scroll(times=times)
            return None

        # 匹配 window.__XXX__（含箭头函数包裹格式）
        if window_var_match:
            var_name = window_var_match.group(1)
            if var_name.startswith("__") and var_name.endswith("__"):
                content = self.get_content()
                result = self._extract_json_var(content, var_name)
                if result is not None:
                    return result
                # 回退：尝试从完整 snapshot 文本中解析 JSON
                return self._extract_json_from_text(content, var_name)

        # 匹配 document.title
        if "document.title" in expression or "document['title']" in expression:
            return self.get_title()

        logger.debug(f"evaluate: 不支持的表达式 '{expression[:80]}'，返回 None")
        return None

    def query_text(self, selector_hint: str = None) -> List[str]:
        """
        从页面快照中提取文本片段。

        通过解析 agent-browser snapshot 输出，按行匹配关键词。
        """
        self._check_closed()
        content = self.get_content()
        if not content:
            return []

        lines = content.split("\n")
        results = []

        if selector_hint:
            hint_lower = selector_hint.lower()
            for line in lines:
                line = line.strip()
                if line and hint_lower in line.lower():
                    results.append(line)
        else:
            results = [line.strip() for line in lines if line.strip()]

        return results

    def close(self):
        """关闭页面（agent-browser 在会话层面管理，这里只是标记）"""
        self._closed = True

    # ── 内部方法 ──

    def _check_closed(self):
        if self._closed:
            raise RuntimeError("页面已关闭")

    def _get_snapshot(self) -> str:
        """获取 agent-browser snapshot 输出"""
        result = _run_cli(["snapshot"], timeout=30, check=False)
        return result.stdout

    @staticmethod
    def _extract_json_var(content: str, var_name: str) -> Optional[dict]:
        """从页面内容中提取 window 变量的 JSON 数据

        匹配模式：
            var_name = {...};
            var_name = {...}
            var_name:{...}
        """
        patterns = [
            rf'{re.escape(var_name)}\s*=\s*(\{{.+?\}});?\s*\n',
            rf'{re.escape(var_name)}\s*=\s*(\{{.+?\}})',
            rf'{re.escape(var_name)}\s*:\s*(\{{.+?\}})',
        ]
        for pattern in patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def _extract_json_from_text(content: str, var_name: str) -> Optional[dict]:
        """
        从快照文本中深度解析 JSON（处理 agent-browser snapshot 格式）。

        策略：在内容中查找 var_name，然后智能匹配大括号配对。
        对大型 JSON（如 SIGI_STATE）使用括号计数法确保完整性。
        """
        # 在内容中定位 var_name
        var_positions = [
            m.start() for m in re.finditer(re.escape(var_name), content)
        ]
        for pos in var_positions:
            # 从 var_name 位置开始，找到后续的第一个 {
            rest = content[pos + len(var_name):]
            brace_start = rest.find('{')
            if brace_start == -1:
                continue

            # 括号计数法：从 { 开始计数，直到配对的 }
            start = pos + len(var_name) + brace_start
            depth = 0
            end = start
            for i in range(start, len(content)):
                ch = content[i]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

            if end > start:
                json_str = content[start:end]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    # 尝试修复常见的 JSON 问题
                    fixed = AgentBrowserPage._try_fix_json(json_str)
                    if fixed is not None:
                        return fixed
                    continue

        return None

    @staticmethod
    def _try_fix_json(json_str: str) -> Optional[dict]:
        """尝试修复格式不正确的 JSON 片段"""
        try:
            # 方法1: 尝试用 json.JSONDecoder.raw_decode（处理尾部额外字符）
            import json as _json
            decoder = _json.JSONDecoder()
            obj, _ = decoder.raw_decode(json_str)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        try:
            # 方法2: 处理单引号 JSON
            fixed = json_str.replace("'", '"')
            return json.loads(fixed)
        except Exception:
            pass

        return None


# ── AgentBrowserSession ──

class AgentBrowserSession(AbstractSession):
    """
    agent-browser 会话。

    agent-browser 使用持久化后台守护进程，open 命令自动连接。
    session 级别管理 daemon 的生命周期。
    """

    def __init__(self, daemon_alive: bool):
        self._daemon_alive = daemon_alive
        self._pages: List[AgentBrowserPage] = []
        self._closed = False

    def new_page(self) -> AgentBrowserPage:
        if self._closed:
            raise RuntimeError("会话已关闭")
        page = AgentBrowserPage(self)
        self._pages.append(page)
        return page

    def close(self):
        """关闭会话中的所有页面"""
        self._closed = True
        for page in self._pages:
            try:
                page.close()
            except Exception:
                pass
        self._pages.clear()


# ── AgentBrowserEngine ──

@register_engine("agent_browser")
class AgentBrowserEngine(BrowserEngine):
    """
    Agent Browser 引擎实现。

    通过 subprocess 调用 agent-browser CLI，
    不依赖 Playwright。

    能力矩阵：
        evaluate:           ❌ (通过内容解析降级)
        network_intercept:  ❌ (上层改用页面解析策略)
        cookies:            ❌ (agent-browser 依赖浏览器自身 Profile)
    """

    ENGINE_NAME = "agent_browser"

    def __init__(self, **kwargs):
        self._launched = False
        self._daemon_started = False

    @property
    def name(self) -> str:
        return self.ENGINE_NAME

    def is_available(self) -> bool:
        return _check_cli()

    def launch(self, **kwargs):
        """
        启动 agent-browser 后台守护进程。

        agent-browser 的 open 命令会自动启动 daemon，
        这里做一个预热调用确保 daemon 就绪。
        """
        if self._launched:
            return

        if not _check_cli():
            raise RuntimeError(
                "agent-browser CLI 不可用。\n"
                "请安装: npm install -g agent-browser && agent-browser install"
            )

        logger.info("AgentBrowserEngine: daemon 将在首次 open 时自动启动")
        self._launched = True

    def session(self, **kwargs) -> BrowserSessionContext:
        """
        创建浏览器会话。

        agent-browser 使用持久化 daemon，
        会话之间共享浏览器进程状态（cookies、localStorage）。
        """
        if not self._launched:
            self.launch(**kwargs)

        session = AgentBrowserSession(daemon_alive=True)
        return BrowserSessionContext(self, session)

    def close(self):
        """关闭 agent-browser daemon"""
        if not self._launched:
            return
        try:
            _run_cli(["close"], timeout=10, check=False)
            logger.info("AgentBrowserEngine: daemon 已关闭")
        except Exception as e:
            logger.warning(f"AgentBrowserEngine: 关闭 daemon 失败: {e}")
        finally:
            self._launched = False
            self._daemon_started = False

    def supports_evaluate(self) -> bool:
        return False  # 仅通过内容解析降级支持

    def supports_network_intercept(self) -> bool:
        return False

    def supports_cookies(self) -> bool:
        return False  # agent-browser 依赖系统 Chrome Profile
