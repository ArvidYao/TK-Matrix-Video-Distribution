# TK 视频自动分发工具

自 v1.1.3 起，新增「一键直达」按钮可在系统文件管理器中快速打开下载目录；批量下载支持实时进度条显示。

## 🎯 解决什么问题

- **68台手机手动传视频太累** — 一键自动分配+传输
- **传完还要手动删文件** — 传输成功自动清理，失败保留重试
- **怕视频重复分配** — 完整历史记录，已分发的不会重复

## ⚡ 快速开始

### 方式一：双击启动（推荐）

双击 **TK视频分发工具.app** → 自动启动 Web 服务 + 打开浏览器访问 http://localhost:5800

> 如遇"无法打开"安全提示：系统设置 → 隐私与安全性 → 仍要打开

### 方式二：命令行启动

```bash
# iOS 设备通信工具链
brew install libimobiledevice usbmuxd

# Python 依赖
pip install pymobiledevice3 pyyaml
```

### 2. 准备视频目录

把 Adobe Media Encoder 转码后的成品放到 `videos_ready/` 目录下：

```
项目目录/
├── videos_ready/           ← 把转码好的视频放这里
│   ├── movie_clip_001.mp4
│   ├── movie_clip_002.mp4
│   └── ...
├── config.yaml             ← 配置文件（可按需修改）
├── tk-distributor.py       ← 主脚本
└── logs/                   ← 日志和分发历史
```

### 3. 修改配置（可选）

编辑 `config.yaml`：

```yaml
# 每台手机传几个视频（默认3个）
videos_per_device: 3

# 是否自动删除已用视频
auto_delete_after_upload: true

# 视频源目录路径
video_source_dir: "./videos_ready"
```

### 4. 运行

```bash
# 正式运行：插上手机后执行
python3 tk-distributor.py

# 仅查看当前连接了哪些设备
python3 tk-distributor.py --list-devices

# 模拟运行（不实际操作，用于测试）
python3 tk-distributor.py --dry-run
```

## 🔧 使用流程

```
1. 插入一批手机（建议每批 ≤20 台）
        ↓
2. 手机弹窗"信任此电脑" → 点击"信任"
        ↓
3. 运行: python3 tk-distributor.py
   脚本自动完成：
   ✓ 检测所有连接的手机
   ✓ 每台手机分配 3 个不同视频
   ✓ 批量上传到各手机
   ✓ 删除电脑上已用的视频
   ✓ 记录完整日志
        ↓
4. 拔掉这批 → 插下一批 → 再运行脚本
```

## 📋 命令说明

| 命令 | 说明 |
|------|------|
| `python3 tk-distributor.py` | 正常运行，自动检测→分配→传输→清理 |
| `python3 tk-distributor.py -l` | 仅列出当前连接的设备 |
| `python3 tk-distributor.py --dry-run` | 模拟运行，不实际传输/删除 |
| `python3 tk-distributor.py -c myconfig.yaml` | 使用自定义配置文件 |

## ⚠️ 注意事项

1. **首次连接**：每台新手机首次连电脑需要在手机上点击"信任"
2. **USB 连接**：使用数据线而非仅充电线；推荐用 USB Hub 批量连接
3. **批次限制**：默认每批最多 20 台设备（可在配置中调整）
4. **视频格式**：支持 .mp4 / .mov / .m4v
5. **防重复机制**：已成功分发的视频会记录在 `logs/distribution_history.json`，不会重复分配

## 📂 文件说明

| 文件/目录 | 用途 |
|----------|------|
| `tk-distributor.py` | 主程序脚本 |
| `config.yaml` | 配置文件（目录、数量、规则等） |
| `videos_ready/` | 待分发的视频存放区 |
| `logs/` | 运行日志 + 分发历史记录 |
| `logs/distribute_YYYY-MM-DD.log` | 当日运行日志 |
| `logs/distribution_history.json` | 分发历史（防重复） |

## ❓ 常见问题

**Q: 提示"未检测到设备"？**
- 确认 USB 数据线已连接（不是纯充电线）
- 确认手机上已点击"信任此电脑"
- 运行 `brew services start usbmuxd` 确保 usbmuxd 服务在运行

**Q: 传输速度慢？**
- USB 2.0 Hub 会比直连慢很多，建议用 USB 3.0 Hub 或分批处理

**Q: 部分视频没删掉？**
- 这是正常行为——只有**传输成功**的视频才会被自动删除，失败的会保留供下次重试
