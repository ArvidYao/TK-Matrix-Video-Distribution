# TK Distributor 长期记忆

## 重大架构变更（2026-05-15）
**弃用爱思助手 GUI 自动化方案**，彻底切换到 **AFC 直传 + IMG_ 命名 + 照片库重建**

原因：爱思助手是 Electron 应用，侧边栏按钮不暴露 macOS Accessibility API，坐标定位不稳。

## 当前技术架构（2026-05-15 最终版）

### 核心文件
| 文件 | 说明 |
|------|------|
| `import_to_iphone.py` | AFC 上传 + 照片库重建核心逻辑 |
| `tk-distributor.py` | CLI 分发入口 |
| `web_app.py` | Web 管理界面（http://localhost:5800）|
| `tk_distributor_core.py` | 视频分配 + 历史记录 |

### 传输引擎
- **主力**: `pymobiledevice3` AFC async API（支持中文路径，~15 MB/s）
- **备用**: `afcclient` CLI（仅限 ASCII 路径，中文路径 stdin 编码损坏）
- 完全不需要爱思助手 GUI

### 文件命名
- iOS 相册只识别 **`IMG_XXXX`** 格式（标准相机命名）
- 自动编号：`_query_next_dcim_number()` 查询设备最大编号后 +1
- `_query_next_dcim_number_by_list()` 批量上传时用 AFC API 直接查

### 照片库重建（唯一验证通过的方式）
1. 重命名 `/PhotoData/Photos.sqlite`（让数据库消失）
2. 通过 `DiagnosticsService.restart()` 重启 iPhone
3. iOS 启动时检测到数据库缺失，自动重建 + 扫描 DCIM
4. 等待 60-90 秒

**注意**：
- NotificationProxy 通知**不足**以触发扫描
- 多次强制重建（>1次）**损坏数据库**，表现为缩略图在但打不开
- 每次分发只做一次重建+重启

### 分发流程（两阶段）
1. **上传阶段**: `upload_videos_batch()` 单次连接批量上传所有文件
2. **重启阶段**: `rebuild_and_restart()` 删 DB + 重启（顺序执行）

### 多设备问题（未解决）
- 5 台设备只有 4 台能成功重建重启
- 第 5 台总是失败，无论具体是哪一台
- 怀疑 usbmuxd 连接资源在第 5 次时耗尽
- 当前方案：连续快速处理（不间隔），`rebuild_and_restart` 含 3 次快速重试

### 配置要点
- `auto_delete_after_upload: false` → 不标记已分发，视频可重复用
- `max_devices_per_batch: 9` → 每次最多 9 台
- 视频目录: `/Users/mac/Desktop/中视频/已剪辑视频/5月13日乜视频都有/`

## 视频文件命名规范（2026-05-13 确认）
- **格式**: `内容名-(变体编号).mp4`
- **示例**: `俾狗追-(1).mp4`、`CUT-(42).mp4`、`肥皂-(10).mp4`
- **规则**:
  - `-` 前面的部分 = **内容名**
  - `-` 后面 `(数字)` = **变体编号**
- **分配逻辑**: 每台设备 3 个不同内容的视频

## 已解决的问题
1. ✅ 中文路径文件名上传 → pymobiledevice3 原生 API（afcclient 编码损坏）
2. ✅ 连续上传断连 → `upload_videos_batch()` 单次连接
3. ✅ 视频不显示 → `rebuild_and_restart()` 删 DB + 重启
4. ✅ 多次重建搞坏数据库 → 仅做一次，不重复
5. ✅ 视频可重复使用 → 保留文件模式不标记已分发
6. ✅ 照片 App 闪退 → 清理堆积的 .bak 文件

## 待解决问题
1. 5 台设备中第 5 台无法重建重启（usbmuxd 资源问题）
