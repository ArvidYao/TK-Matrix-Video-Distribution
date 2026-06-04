#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器引擎迁移验证脚本
======================
检查新引擎架构是否正确安装和配置。

用法： python3 verify_engine_migration.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

def check_banner(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def check_ok(msg):
    print(f"  ✅ {msg}")

def check_fail(msg):
    print(f"  ❌ {msg}")

def check_warn(msg):
    print(f"  ⚠️  {msg}")

def main():
    check_banner("浏览器引擎迁移验证")

    # ── 1. 抽象层是否可导入 ──
    check_banner("1. 抽象层检查")
    try:
        from browser_engine import BrowserEngine, EngineCapabilities
        check_ok("browser_engine 导入成功")
    except ImportError as e:
        check_fail(f"browser_engine 导入失败: {e}")
        return 1

    # ── 2. 引擎注册表 ──
    check_banner("2. 已注册引擎")
    engines = BrowserEngine.list_engines()
    if engines:
        for name in engines:
            check_ok(f"引擎已注册: {name}")
    else:
        check_fail("没有注册任何引擎！")

    # ── 3. agent-browser CLI 可用性 ──
    check_banner("3. Agent Browser 引擎")
    try:
        from engines.agent_browser_engine import AgentBrowserEngine, _check_cli
        agent_engine = AgentBrowserEngine()
        if agent_engine.is_available():
            check_ok("agent-browser CLI 可用")
        else:
            check_warn("agent-browser CLI 不可用，将自动降级到 Playwright")
            check_warn("安装方法: npm install -g agent-browser && agent-browser install")
    except ImportError as e:
        check_fail(f"导入失败: {e}")

    # ── 4. Playwright 引擎可用性 ──
    check_banner("4. Playwright 引擎（降级引擎）")
    try:
        from engines.playwright_engine import PlaywrightEngine
        pw_engine = PlaywrightEngine()
        if pw_engine.is_available():
            check_ok("Playwright 可用（降级引擎就绪）")
        else:
            check_warn("Playwright 未安装")
            check_warn("安装方法: pip install playwright && python -m playwright install chrome")
    except ImportError as e:
        check_warn(f"导入失败: {e}")

    # ── 5. TikTok 爬虫兼容性 ──
    check_banner("5. TikTok 爬虫兼容性")
    try:
        from tiktok_scraper import TikTokScraper, extract_from_api_response
        check_ok("TikTokScraper 导入成功")
        # 检查是否支持新参数
        scraper = TikTokScraper(engine_name="auto")
        check_ok(f"TikTokScraper 实例化成功 (engine_name={scraper.engine_name})")
    except ImportError as e:
        check_fail(f"导入失败: {e}")
    except Exception as e:
        check_fail(f"实例化失败: {e}")

    # ── 6. 自动引擎选择 ──
    check_banner("6. 自动引擎选择")
    try:
        engine = BrowserEngine.create("auto")
        caps = EngineCapabilities(engine)
        check_ok(f"自动选择引擎: {caps.name}")
        check_ok(f"  能力: {caps.as_dict()}")
    except RuntimeError as e:
        check_fail(f"自动选择失败: {e}")
        check_warn("请至少安装 agent-browser 或 playwright 中的一个")

    # ── 7. Web 端兼容性 ──
    check_banner("7. Web 端 (web_app.py) 兼容性")
    try:
        # 模拟 web_app.py 的导入逻辑
        import tiktok_scraper as ts
        from browser_engine import BrowserEngine as BE
        has_scraper = True
        available_engines = BE.list_engines()
        check_ok(f"web_app 兼容: scraper={has_scraper}, engines={available_engines}")
    except Exception as e:
        check_fail(f"web_app 兼容性检查失败: {e}")

    # ── 汇总 ──
    check_banner("验证完成")
    print(f"\n  可用引擎数: {len(engines)}")
    print(f"  引擎列表: {engines}")

    # 提供迁移建议
    if "agent_browser" in engines and "playwright" in engines:
        print(f"\n  ✅ 双引擎就绪，agent-browser 为主引擎，Playwright 为降级引擎")
    elif "agent_browser" in engines:
        print(f"\n  ✅ agent-browser 引擎就绪")
    elif "playwright" in engines:
        print(f"\n  ⚠️  仅 Playwright 可用，建议安装 agent-browser 以获得完整能力")
    else:
        print(f"\n  ❌ 无可用引擎！请安装 agent-browser 或 playwright")

    return 0

if __name__ == "__main__":
    sys.exit(main())
