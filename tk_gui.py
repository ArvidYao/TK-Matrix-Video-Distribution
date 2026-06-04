#!/usr/bin/env python3
"""
TK 视频分发工具 v1.1.3 - 桌面版
作者：姐夫
"""

import sys, os, json, time, threading, subprocess, yaml
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))
CONFIG_FILE = BASE_DIR / "config.yaml"


class TKDistributorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"TK 视频分发工具 v1.1.3")
        self.root.geometry("880x720")
        self.root.minsize(750, 600)
        self.root.configure(bg="#f5f5f7")

        self.distributing = False
        self.stop_requested = False
        self.source_dir = BASE_DIR

        self._build_ui()
        self._load_config()
        self._refresh_devices()
        self._refresh_videos()

    # ═══════════════════════════════════════════
    #  UI 构建
    # ═══════════════════════════════════════════

    def _make_frame(self, parent, title=None, padding=10):
        if title:
            f = ttk.LabelFrame(parent, text=title, padding=padding)
        else:
            f = ttk.Frame(parent)
        return f

    def _build_ui(self):
        # ── 顶栏 ──
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=16, pady=(12, 4))
        ttk.Label(top, text="TK 视频分发工具",
                  font=("Helvetica", 18, "bold")).pack(side=tk.LEFT)
        ttk.Label(top, text="v1.1.3  作者：姐夫",
                  font=("Helvetica", 11), foreground="#888").pack(side=tk.LEFT, padx=10)

        # ── 视频源目录 ──
        dir_frame = ttk.LabelFrame(self.root, text="视频源", padding=8)
        dir_frame.pack(fill=tk.X, padx=16, pady=4)

        dir_row = ttk.Frame(dir_frame)
        dir_row.pack(fill=tk.X)
        self.dir_path_var = tk.StringVar()
        self.dir_entry = ttk.Entry(dir_row, textvariable=self.dir_path_var,
                                   font=("Menlo", 10))
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dir_row, text="📁 选择目录",
                   command=self._choose_dir).pack(side=tk.RIGHT, padx=(8, 0))

        # ── 中间列：视频列表 + 设备 ──
        mid_frame = ttk.Frame(self.root)
        mid_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        mid_frame.columnconfigure(0, weight=1)
        mid_frame.columnconfigure(1, weight=1)
        mid_frame.rowconfigure(0, weight=1)

        # ── 视频列表（左） ──
        video_frame = ttk.LabelFrame(mid_frame, text="可用视频", padding=4)
        video_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        self.video_listbox = tk.Listbox(video_frame, font=("Menlo", 10),
                                        height=8, selectmode=tk.NONE)
        vsb = ttk.Scrollbar(video_frame, orient=tk.VERTICAL,
                            command=self.video_listbox.yview)
        self.video_listbox.configure(yscrollcommand=vsb.set)
        self.video_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        vinfo = ttk.Frame(video_frame)
        vinfo.pack(fill=tk.X)
        self.video_count_label = ttk.Label(vinfo, text="0 个视频",
                                           font=("Helvetica", 9))
        self.video_count_label.pack(side=tk.LEFT, padx=4)

        # ── 设备列表（右） ──
        dev_frame = ttk.LabelFrame(mid_frame, text="已连接设备", padding=4)
        dev_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        dev_top = ttk.Frame(dev_frame)
        dev_top.pack(fill=tk.X)
        self.dev_count_label = ttk.Label(dev_top, text="0 台",
                                         font=("Helvetica", 9))
        self.dev_count_label.pack(side=tk.LEFT, padx=4)
        ttk.Button(dev_top, text="刷新",
                   command=self._refresh_all).pack(side=tk.RIGHT)

        self.dev_listbox = tk.Listbox(dev_frame, font=("Menlo", 10),
                                      height=8)
        self.dev_listbox.pack(fill=tk.BOTH, expand=True)

        # ── 设置 ──
        cfg_frame = ttk.LabelFrame(self.root, text="设置", padding=8)
        cfg_frame.pack(fill=tk.X, padx=16, pady=4)

        cfg_row = ttk.Frame(cfg_frame)
        cfg_row.pack(fill=tk.X)

        self.auto_del = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg_row, text="分发后自动删除本地视频",
                        variable=self.auto_del).pack(side=tk.LEFT, padx=4)

        ttk.Separator(cfg_row, orient=tk.VERTICAL).pack(side=tk.LEFT,
                                                         fill=tk.Y, padx=12)

        ttk.Label(cfg_row, text="每台").pack(side=tk.LEFT)
        self.vid_per_dev = ttk.Combobox(cfg_row, width=4,
                                        values=["2", "3", "4", "5"])
        self.vid_per_dev.set("3")
        self.vid_per_dev.pack(side=tk.LEFT, padx=2)
        ttk.Label(cfg_row, text="个视频").pack(side=tk.LEFT)

        ttk.Separator(cfg_row, orient=tk.VERTICAL).pack(side=tk.LEFT,
                                                         fill=tk.Y, padx=12)
        ttk.Label(cfg_row, text="批次").pack(side=tk.LEFT)
        self.batch_combo = ttk.Combobox(cfg_row, width=4,
                                        values=["2", "3", "4", "5", "9"])
        self.batch_combo.set("4")
        self.batch_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(cfg_row, text="台").pack(side=tk.LEFT)

        # ── 控制按钮 ──
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill=tk.X, padx=16, pady=4)

        self.start_btn = ttk.Button(ctrl, text="▶ 开始分发",
                                    command=self._start_distribute, width=18)
        self.start_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(ctrl, text="⏹ 停止",
                                   state=tk.DISABLED,
                                   command=self._stop_distribute, width=10)
        self.stop_btn.pack(side=tk.LEFT, padx=8)

        # ── 进度条 ──
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill=tk.X, padx=16, pady=4)

        # ── 日志 ──
        log_frame = ttk.LabelFrame(self.root, text="日志", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 12))

        self.log = scrolledtext.ScrolledText(
            log_frame, font=("Menlo", 10), wrap=tk.WORD,
            state=tk.DISABLED, bg="#1e1e2e", fg="#cdd6f4",
            insertbackground="white", relief=tk.FLAT, bd=0)
        self.log.pack(fill=tk.BOTH, expand=True)

    # ═══════════════════════════════════════════
    #  功能
    # ═══════════════════════════════════════════

    def _choose_dir(self):
        d = filedialog.askdirectory(
            title="选择视频源文件夹",
            initialdir=str(self.source_dir) if self.source_dir else "/")
        if d:
            self.source_dir = Path(d)
            self.dir_path_var.set(str(self.source_dir))
            self._refresh_videos()
            self._save_config()

    def _log(self, msg):
        def _do():
            self.log.config(state=tk.NORMAL)
            ts = datetime.now().strftime("%H:%M:%S")
            self.log.insert(tk.END, f"[{ts}] {msg}\n")
            self.log.see(tk.END)
            self.log.config(state=tk.DISABLED)
        self.root.after(0, _do)

    def _refresh_all(self):
        self._refresh_devices()
        self._refresh_videos()

    def _refresh_devices(self):
        self.dev_listbox.delete(0, tk.END)
        try:
            r = subprocess.run(["idevice_id", "-l"],
                               capture_output=True, text=True, timeout=5)
            udids = [u.strip() for u in r.stdout.strip().split("\n") if u.strip()]
            self.dev_count_label.config(text=f"{len(udids)} 台")
            for i, u in enumerate(udids, 1):
                self.dev_listbox.insert(tk.END, f"  #{i:<3} {u[:24]}")
            if not udids:
                self.dev_listbox.insert(tk.END, "  未检测到设备")
        except Exception:
            self.dev_listbox.insert(tk.END, "  检测失败")

    def _refresh_videos(self):
        self.video_listbox.delete(0, tk.END)
        if not self.source_dir or not self.source_dir.exists():
            self.video_count_label.config(text="目录不存在")
            return
        videos = sorted(self.source_dir.glob("*.mp4"))
        for v in videos[:50]:
            size_mb = v.stat().st_size / (1024 * 1024)
            self.video_listbox.insert(tk.END,
                                      f"  {v.name:<35s} {size_mb:5.1f}MB")
        total = len(videos)
        shown = min(total, 50)
        self.video_count_label.config(
            text=f"{total} 个视频{' (显示前50)' if total > 50 else ''}")

    def _load_config(self):
        try:
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f) or {}
            src = cfg.get("video_source_dir")
            if src:
                self.source_dir = Path(src)
                self.dir_path_var.set(str(self.source_dir))
            self.auto_del.set(cfg.get("auto_delete_after_upload", True))
            self.vid_per_dev.set(str(cfg.get("videos_per_device", 3)))
            self.batch_combo.set(str(min(cfg.get("max_devices_per_batch", 9), 9)))
        except Exception:
            if not self.dir_path_var.get():
                self.dir_path_var.set(str(BASE_DIR))

    def _save_config(self):
        try:
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f) or {}
            cfg["video_source_dir"] = str(self.source_dir)
            cfg["auto_delete_after_upload"] = self.auto_del.get()
            cfg["videos_per_device"] = int(self.vid_per_dev.get())
            cfg["max_devices_per_batch"] = min(int(self.batch_combo.get()), 9)
            with open(CONFIG_FILE, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        except Exception:
            pass

    def _start_distribute(self):
        if self.distributing:
            return
        self.distributing = True
        self.stop_requested = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress.start()
        self._save_config()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _stop_distribute(self):
        self.stop_requested = True
        self._log("⏹ 用户请求停止")

    def _finish(self, ok_count, total):
        def _do():
            self.distributing = False
            self.progress.stop()
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self._refresh_devices()
            self._refresh_videos()
            self._log(f"✅ {ok_count}/{total} 台成功")
        self.root.after(0, _do)

    def _run(self):
        try:
            self._log("🔍 扫描设备...")
            r = subprocess.run(["idevice_id", "-l"],
                               capture_output=True, text=True, timeout=5)
            udids = [u.strip() for u in r.stdout.strip().split("\n") if u.strip()]
            if not udids:
                self._log("❌ 未检测到设备")
                self._finish(0, 0)
                return
            self._log(f"✅ {len(udids)} 台设备")
            devices = [{"name": f"设备#{i+1}", "udid": u}
                       for i, u in enumerate(udids)]

            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f) or {}
            from tk_distributor_core import VideoAllocator
            import logging
            allocator = VideoAllocator(cfg, logging.getLogger("gui"))

            available = allocator.get_available_videos()
            needed = len(devices) * int(self.vid_per_dev.get())
            self._log(f"📦 可用: {len(available)} 个, 需: {needed} 个")
            if len(available) < needed:
                self._log("⚠️ 视频不足")
                devices = devices[:max(1, len(available) // int(self.vid_per_dev.get()))]

            allocations = allocator.allocate(devices)
            if not allocations:
                self._log("❌ 无可用视频")
                self._finish(0, 0)
                return

            dev_list = list(allocations.items())
            success_devs = []

            for idx, (udid, videos) in enumerate(dev_list):
                if self.stop_requested:
                    break
                device = next(d for d in devices if d["udid"] == udid)
                name = device["name"]
                self._log(f"📤 [{idx+1}/{len(dev_list)}] {name}...")

                from import_to_iphone import upload_videos_batch
                results = upload_videos_batch(udid, videos)
                ok = True
                for vp, r in zip(videos, results):
                    if r:
                        self._log(f"  ✅ {vp.name}")
                        if self.auto_del.get():
                            try:
                                vp.unlink()
                                self._log(f"  🗑️ 删除: {vp.name}")
                            except Exception:
                                pass
                    else:
                        self._log(f"  ❌ {vp.name} 失败")
                        ok = False
                if ok:
                    success_devs.append((udid, name, device))

            # 并行 CLI 重启
            if success_devs and not self.stop_requested:
                self._log(f"\n🔄 重启 {len(success_devs)} 台...")
                res = {}
                lock = threading.Lock()

                def _reboot(udid, name):
                    for _try in range(5):
                        ts = str(int(time.time()))
                        for _f in ['Photos.sqlite', 'Photos.sqlite-wal', 'Photos.sqlite-shm']:
                            subprocess.run(['afcclient', '-u', udid],
                                           input=f"mv /PhotoData/{_f} /PhotoData/{_f}.cli_{ts}\nexit\n",
                                           capture_output=True, text=True, timeout=10)
                        r = subprocess.run(['idevicediagnostics', '-u', udid, 'restart'],
                                           capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            with lock:
                                res[udid] = True
                            return
                        time.sleep(1)
                    with lock:
                        res[udid] = False

                threads = [threading.Thread(target=_reboot, args=(u, n), daemon=True)
                           for u, n, _ in success_devs]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)

                ok_count = sum(1 for u, n, _ in success_devs if res.get(u))
                for u, n, _ in success_devs:
                    self._log(f"  {'✅' if res.get(u) else '❌'} {n}")

                if ok_count > 0:
                    self._log(f"⏳ 等 90 秒重启...")
                    time.sleep(90)

            self._log(f"\n✅ 完成")
            self._finish(len(success_devs), len(devices))

        except Exception as e:
            self._log(f"❌ 异常: {type(e).__name__}: {e}")
            import traceback
            self._log(traceback.format_exc()[:300])
            self._finish(0, 0)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    TKDistributorApp().run()
