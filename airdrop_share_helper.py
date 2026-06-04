#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AirDrop 分享辅助脚本
====================
独立进程，正确实现 NSApplication 事件循环 + NSSharingServiceDelegate，
确保 AirDrop 分享面板能正常弹出（而非文件选择对话框）。

用法: python3 airdrop_share_helper.py <文件路径>

关键改进（参考 vldmrkl/airdrop-cli 的 Swift 实现）：
- 使用 NSApplication + delegate + app.run() 进入事件循环
- 在 applicationDidFinishLaunching_ 中触发分享
- 实现 NSSharingServiceDelegate 提供源窗口（sourceWindow）
- 分享完成/失败后通过 app.terminate_ 退出

为什么之前弹出了文件选择对话框：
- NSSharingService.performWithItems_ 需要一个活的 NSApplication 事件循环
  和一个锚定窗口来展示分享面板
- 没有事件循环和窗口时，系统回退到显示文件选择对话框
"""

import sys
import os
from pathlib import Path

os.environ['PYTHONUNBUFFERED'] = '1'

import AppKit
from Foundation import NSObject, NSRect, NSPoint, NSSize
from objc import super as objc_super


class AirDropHelper(NSObject):
    """同时实现 NSApplicationDelegate 和 NSSharingServiceDelegate"""

    def initWithFile_(self, file_path):
        self = objc_super(AirDropHelper, self).init()
        if self is None:
            return None
        self.file_path = file_path
        self.window = None
        return self

    # ---- NSApplicationDelegate ----

    def applicationDidFinishLaunching_(self, notification):
        """App 启动后立即触发 AirDrop 分享"""
        url = AppKit.NSURL.fileURLWithPath_(self.file_path)
        if not url:
            print(f"[AirDrop Helper] ✗ 无法创建 URL: {self.file_path}", flush=True)
            self._terminate()
            return

        service = AppKit.NSSharingService.sharingServiceNamed_(
            "com.apple.share.AirDrop.send"
        )

        if service is None:
            print("[AirDrop Helper] ✗ AirDrop 服务不可用", flush=True)
            self._terminate()
            return

        if not service.canPerformWithItems_([url]):
            print(f"[AirDrop Helper] ✗ 文件类型不支持 AirDrop", flush=True)
            self._terminate()
            return

        service.setDelegate_(self)
        service.performWithItems_([url])
        print(f"[AirDrop Helper] ✓ AirDrop 分享面板已弹出: {Path(self.file_path).name}", flush=True)

    # ---- NSSharingServiceDelegate ----

    def sharingService_sourceWindowForShareItems_sharingContentScope_(
        self, service, items, contentScope
    ):
        """提供锚定窗口 —— 分享面板需要附着在窗口上，定位于屏幕中央"""
        if self.window is None:
            # 获取主屏幕尺寸，计算中央位置
            screen = AppKit.NSScreen.mainScreen()
            if screen is not None:
                screen_rect = screen.frame()
                center_x = screen_rect.origin.x + screen_rect.size.width / 2
                center_y = screen_rect.origin.y + screen_rect.size.height / 2
            else:
                center_x, center_y = 500, 500
            frame = NSRect(NSPoint(center_x, center_y), NSSize(1, 1))
            style_mask = AppKit.NSWindowStyleMaskBorderless
            self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, style_mask, 2, False  # 2 = NSBackingStoreBuffered
            )
            self.window.setLevel_(AppKit.NSFloatingWindowLevel)
            self.window.orderFront_(None)
        return self.window

    def sharingService_didShareItems_(self, service, items):
        """分享成功回调"""
        print("[AirDrop Helper] ✓ 分享成功", flush=True)
        self._terminate()

    def sharingService_didFailToShareItems_error_(self, service, items, error):
        """分享失败回调"""
        err_msg = error.localizedDescription() if error else "未知原因"
        print(f"[AirDrop Helper] ✗ 分享失败: {err_msg}", flush=True)
        self._terminate()

    # ---- 内部 ----

    def _terminate(self):
        """安全终止应用，退出事件循环"""
        try:
            app = AppKit.NSApp()
            if app is not None:
                app.terminate_(None)
        except Exception:
            os._exit(0)


def main():
    if len(sys.argv) < 2:
        print("用法: python3 airdrop_share_helper.py <文件路径>", file=sys.stderr)
        sys.exit(1)

    file_path = sys.argv[1]
    file = Path(file_path)

    if not file.exists():
        print(f"错误: 文件不存在 - {file_path}", file=sys.stderr)
        sys.exit(1)

    # 初始化 NSApplication（遵循 airdrop-cli 的正确方式）
    app = AppKit.NSApplication.sharedApplication()

    helper = AirDropHelper.alloc().initWithFile_(file_path)
    if helper is None:
        print("错误: 无法创建 AirDropHelper", file=sys.stderr)
        sys.exit(1)

    app.setDelegate_(helper)
    app.setActivationPolicy_(0)       # NSApplicationActivationPolicyRegular
    app.activateIgnoringOtherApps_(True)

    # 进入事件循环 —— 阻塞直到 terminate 被调用
    app.run()


if __name__ == "__main__":
    main()
