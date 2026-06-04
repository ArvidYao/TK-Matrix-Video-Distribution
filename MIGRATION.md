# 浏览器引擎迁移指南

## 概述

v2.0 将 TikTok 视频爬虫的浏览器自动化层从**直接 Playwright 调用**重构为**引擎抽象架构**。

```
┌─────────────────────┐
│   TikTokScraper     │  ← 业务逻辑，零改动
├─────────────────────┤
│   BrowserEngine     │  ← 抽象接口层
├─────────────────────┤
│ AgentBrowser │ Playwright  │  ← 双引擎实现
└─────────────────────┘
```

## 新文件结构

```
TK视频分发工具/
├── browser_engine.py          ← 抽象基类 + 引擎工厂 (新增)
├── engines/
│   ├── __init__.py            ← 引擎包入口 (新增)
│   ├── agent_browser_engine.py ← agent-browser CLI 封装 (新增)
│   └── playwright_engine.py   ← Playwright 适配器 (新增)
├── tiktok_scraper.py          ← [重构] 使用抽象层替代 Playwright
├── web_app.py                 ← [微小改动] 适配引擎选择
├── tk_distributor_app.py      ← [无改动] 不涉及浏览器操作
├── verify_engine_migration.py ← 迁移验证脚本 (新增)
└── ... (其他文件不变)
```

## 安装 agent-browser（推荐）

```bash
# 安装 CLI 工具
npm install -g agent-browser

# 安装 Chromium 浏览器
agent-browser install
```

安装后验证：
```bash
agent-browser --version
```

## 引擎选择策略

| 模式 | 行为 |
|------|------|
| `auto` (默认) | 自动选择：agent-browser > playwright |
| `agent_browser` | 强制使用 agent-browser CLI |
| `playwright` | 强制使用 Playwright（降级引擎） |

## 从旧版本迁移

### 对现有代码的影响

**tk_distributor_app.py** — 无需任何改动（不涉及浏览器操作）

**web_app.py** — 微小改动：
- 爬虫启动 API 新增 `engine_name` 参数（可选，默认 "auto"）
- 爬虫状态 API 返回 `engines` 字段

**tiktok_scraper.py** — 内部重构：
- 不再直接导入 `playwright.sync_api`
- 改用 `from browser_engine import BrowserEngine`
- 所有公开 API 保持兼容

### 如果你之前直接使用了 TikTokScraper

```python
# 旧代码（仍然兼容）
from tiktok_scraper import TikTokScraper
scraper = TikTokScraper(
    ms_token="xxx",
    proxy="http://127.0.0.1:7890",
    hashtags=["Euphoria"],
)
results, qualified = scraper.run()

# 新代码（可指定引擎）
scraper = TikTokScraper(
    ms_token="xxx",
    proxy="http://127.0.0.1:7890",
    hashtags=["Euphoria"],
    engine_name="agent_browser",  # 新增参数
)
results, qualified = scraper.run()
```

## 验证迁移

```bash
python3 verify_engine_migration.py
```

预期输出：
```
✅ 引擎已注册: agent_browser
✅ 引擎已注册: playwright
✅ TikTokScraper 导入成功
✅ 自动选择引擎: playwright (或 agent_browser)
✅ 双引擎就绪
```

## 能力对比

| 能力 | agent-browser | Playwright (降级) |
|------|:---:|:---:|
| 页面导航 | ✅ | ✅ |
| 内容获取 | ✅ (snapshot) | ✅ (content) |
| 页面滚动 | ✅ | ✅ |
| JS evaluate | ⚠️ 降级为文本解析 | ✅ 原生支持 |
| 网络拦截 | ❌ | ✅ |
| Cookie 注入 | ❌ | ✅ |
| 截图 | ✅ | ✅ |
| 反爬能力 | ✅ (强) | ⚠️ 一般 |

## 提取策略自适应

引擎会自动根据能力调整数据提取策略：

| 引擎 | Layer 1 (API拦截) | Layer 2 (SIGI_STATE) | Layer 3 (文本提取) |
|------|:---:|:---:|:---:|
| Playwright | ✅ 原生拦截 | ✅ JS evaluate | ✅ |
| agent-browser | ❌ 跳过 | ⚠️ 快照文本解析 | ✅ 快照文本解析 |

三层提取是串联降级的：Layer 1 无数据 → Layer 2 → Layer 3

## 回滚方案

如果需要回退到旧版本：

```bash
git checkout HEAD~1 -- tiktok_scraper.py web_app.py
rm browser_engine.py
rm -rf engines/
```

旧版本 tiktok_scraper.py 已备份在：
```
logs/tiktok_scraper.py.backup_$(date +%Y%m%d)
```

## 常见问题

**Q: agent-browser 未安装，爬虫能正常工作吗？**

A: 能。系统会自动降级到 Playwright 引擎，功能完全一致。

**Q: agent-browser 没有 JS evaluate，SIGI_STATE 提取会失败吗？**

A: 已做降级处理。agent-browser 通过解析 snapshot 文本中的 JSON 数据来提取 SIGI_STATE，经测试效果与原生 JS evaluate 一致。

**Q: 如何指定使用哪个引擎？**

- Web 端：在爬虫启动请求中传 `"engine_name": "playwright"`
- 代码：`TikTokScraper(engine_name="playwright")`

**Q: ms_token Cookie 在 agent-browser 模式下生效吗？**

A: agent-browser 依赖系统 Chrome 的本地 Profile，Cookie 由浏览器自身管理。如需注入 Cookie，建议使用 Playwright 引擎。
