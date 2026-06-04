#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yt-dlp 环境诊断与自动修复工具
===============================
检测 yt-dlp 安装状态，排查环境不一致、PATH 缺失等常见问题，
并提供一键修复方案。

用法:
    python3 diagnose_ytdlp.py              # 仅诊断
    python3 diagnose_ytdlp.py --fix        # 诊断并自动修复
    python3 diagnose_ytdlp.py --fix --yes  # 自动确认所有修复操作
"""

import os
import sys
import shutil
import subprocess
import platform
import json
import argparse
from pathlib import Path


# ============================================================
# 样式输出
# ============================================================

HEADER = "\033[1;36m"     # 青色加粗
OK = "\033[1;32m"         # 绿色
WARN = "\033[1;33m"       # 黄色
ERROR = "\033[1;31m"      # 红色
INFO = "\033[0;36m"       # 青色
DIM = "\033[2;37m"        # 灰色
RESET = "\033[0m"
BOLD = "\033[1m"

# Windows CMD 不支持 ANSI，做降级处理
if platform.system() == "Windows":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        HEADER = OK = WARN = ERROR = INFO = DIM = RESET = BOLD = ""


def section(title: str):
    print(f"\n{HEADER}{'=' * 60}{RESET}")
    print(f"{HEADER}  {title}{RESET}")
    print(f"{HEADER}{'=' * 60}{RESET}")


def ok(msg: str):
    print(f"  {OK}✓{RESET} {msg}")


def warn(msg: str):
    print(f"  {WARN}⚠{RESET} {msg}")


def err(msg: str):
    print(f"  {ERROR}✗{RESET} {msg}")


def info(msg: str):
    print(f"  {INFO}→{RESET} {msg}")


def dim(msg: str):
    print(f"  {DIM}{msg}{RESET}")


# ============================================================
# 诊断函数
# ============================================================

def diagnose_system() -> dict:
    """诊断系统基本信息"""
    section("1. 系统环境")
    result = {}

    # OS
    os_name = platform.system()
    os_version = platform.version()
    result["os"] = os_name
    info(f"操作系统: {os_name} ({platform.release()})")
    dim(f"  版本: {os_version}")

    # Shell
    shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown"))
    result["shell"] = shell
    info(f"当前 Shell: {shell}")

    # Python
    py_version = sys.version
    py_executable = sys.executable
    result["python_version"] = py_version.split()[0]
    result["python_executable"] = py_executable
    info(f"Python 版本: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    dim(f"  可执行文件: {py_executable}")

    # 检测是否在虚拟环境
    in_venv = (
        hasattr(sys, 'real_prefix') or
        (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
    )
    result["in_virtualenv"] = in_venv
    if in_venv:
        ok(f"检测到虚拟环境: {sys.prefix}")
    else:
        dim(f"系统级 Python: {sys.prefix}")

    # PATH
    path = os.environ.get("PATH", "")
    result["path"] = path
    path_dirs = [d for d in path.split(os.pathsep) if d]
    info(f"PATH 共 {len(path_dirs)} 个目录")

    return result


def diagnose_pip() -> dict:
    """诊断 pip 安装情况"""
    section("2. pip 环境检测")
    result = {"pip_commands": [], "pip_to_python": {}}

    # 检测各种 pip 变体
    is_windows = platform.system() == "Windows"
    candidates = ["pip3", "pip", "pip3.11", "pip3.12", "pip3.13"]
    if is_windows:
        candidates = ["pip", "pip3"] + candidates

    for cmd in candidates:
        which_path = shutil.which(cmd)
        if which_path:
            result["pip_commands"].append(cmd)
            dim(f"  {cmd}: {which_path}")

            # 获取 pip 版本和关联的 Python
            try:
                output = subprocess.check_output(
                    [cmd, "--version"],
                    stderr=subprocess.STDOUT,
                    timeout=5,
                    text=True,
                )
                version_line = output.strip()
                dim(f"    版本: {version_line}")

                # 解析关联的 Python 版本
                # pip 23.x 输出格式: pip 23.0 from /path/to/site-packages (python 3.12)
                import re
                py_match = re.search(r'python\s+(\d+\.\d+)', version_line, re.IGNORECASE)
                if py_match:
                    result["pip_to_python"][cmd] = py_match.group(1)
                    dim(f"    关联 Python: {py_match.group(1)}")
            except Exception as e:
                dim(f"    (无法获取版本: {e})")

    # 对比 pip 与当前 Python
    current_py = f"{sys.version_info.major}.{sys.version_info.minor}"
    section("2.1 pip ↔ Python 一致性检查")

    mismatch_found = False
    for cmd, py_ver in result.get("pip_to_python", {}).items():
        if py_ver != current_py:
            mismatch_found = True
            warn(f"{cmd} 关联 Python {py_ver}，但当前是 Python {current_py}")
            warn(f"  → 用 '{cmd} install yt-dlp' 安装的包可能不会对当前 Python 生效")

    if not mismatch_found:
        ok(f"所有 pip 命令与当前 Python {current_py} 一致")

    result["mismatch"] = mismatch_found
    return result


def diagnose_ytdlp_cli() -> dict:
    """诊断 yt-dlp 命令行工具"""
    section("3. yt-dlp 命令行检测")
    result = {"cli_found": False, "cli_paths": []}

    candidates = ["yt-dlp", "ytdlp"]
    for cmd in candidates:
        which_path = shutil.which(cmd)
        if which_path:
            result["cli_found"] = True
            result["cli_paths"].append(which_path)
            ok(f"找到 yt-dlp 命令: {which_path}")

            # 获取版本
            try:
                output = subprocess.check_output(
                    [cmd, "--version"],
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    text=True,
                )
                result["version"] = output.strip()
                ok(f"  版本: {output.strip()}")
            except Exception as e:
                warn(f"  无法运行 --version: {e}")

            # 检查是否是可执行脚本
            if os.path.exists(which_path):
                dim(f"  文件大小: {os.path.getsize(which_path)} bytes")
                try:
                    with open(which_path, 'r') as f:
                        first_line = f.readline().strip()
                    dim(f"  Shebang: {first_line}")
                except Exception:
                    pass

    if not result["cli_found"]:
        err("未找到 yt-dlp 命令行工具")
        result["version"] = None

    return result


def diagnose_ytdlp_module() -> dict:
    """诊断 yt-dlp Python 模块"""
    section("4. yt-dlp Python 模块检测")
    result = {"module_found": False, "module_path": None, "module_version": None}

    try:
        import yt_dlp
        result["module_found"] = True
        result["module_path"] = yt_dlp.__file__
        result["module_version"] = getattr(yt_dlp, '__version__', 'unknown')
        ok(f"Python 模块已安装: yt-dlp {result['module_version']}")
        dim(f"  路径: {result['module_path']}")
    except ImportError:
        err("当前 Python 解释器无法导入 yt_dlp 模块")

        # 检查是否安装到了其他 Python
        section("4.1 搜索其他 Python 环境中的 yt-dlp")

        # 尝试 python3 -c "import yt_dlp; print(yt_dlp.__file__)"
        try:
            output = subprocess.check_output(
                ["python3", "-c", "import yt_dlp; print(yt_dlp.__file__)"],
                stderr=subprocess.STDOUT,
                timeout=10,
                text=True,
            )
            path = output.strip()
            if path:
                warn(f"yt-dlp 已安装，但在另一个 Python 环境中！")
                warn(f"  安装位置: {path}")
                warn(f"  当前 Python: {sys.executable}")
                warn(f"  → 请用 '{sys.executable} -m pip install yt-dlp' 重新安装")
                result["other_python_path"] = path
        except Exception:
            dim("  (也未在其他 python3 环境中找到)")

    return result


def diagnose_site_packages() -> dict:
    """诊断 site-packages 中的 yt-dlp 安装"""
    section("5. site-packages 文件扫描")
    result = {"paths_scanned": [], "found_files": []}

    # 收集所有 site-packages 路径
    site_dirs = set()
    for p in sys.path:
        p = Path(p)
        if p.exists() and (p.name == "site-packages" or "site-packages" in str(p)):
            site_dirs.add(str(p))

    # 也扫描 pip show 的输出路径
    try:
        output = subprocess.check_output(
            [sys.executable, "-m", "pip", "show", "yt-dlp"],
            stderr=subprocess.STDOUT,
            timeout=10,
            text=True,
        )
        for line in output.splitlines():
            if line.startswith("Location:"):
                loc = line.split(":", 1)[1].strip()
                site_dirs.add(loc)
    except Exception:
        pass

    for site_dir in sorted(site_dirs):
        result["paths_scanned"].append(site_dir)
        dim(f"  扫描: {site_dir}")

        # 查找 yt_dlp 目录
        ytdlp_dir = os.path.join(site_dir, "yt_dlp")
        if os.path.isdir(ytdlp_dir):
            ok(f"  → 找到 yt_dlp 包目录: {ytdlp_dir}")
            result["found_files"].append(ytdlp_dir)
            # 统计文件数
            py_count = sum(1 for _ in Path(ytdlp_dir).rglob("*.py"))
            dim(f"    包含 {py_count} 个 .py 文件")

        # 查找 yt-dlp 可执行文件包装
        for name in ["yt-dlp", "ytdlp"]:
            wrapper = os.path.join(site_dir, "..", "..", "bin", name)
            if os.path.exists(wrapper):
                ok(f"  → 找到可执行包装: {wrapper}")
                result["found_files"].append(wrapper)

    if not result["found_files"]:
        err("未在任何 site-packages 中找到 yt-dlp 安装")

    return result


def diagnose_path_issue() -> dict:
    """诊断 PATH 环境变量是否包含 yt-dlp 安装路径"""
    section("6. PATH 包含性检查")
    result = {"missing_from_path": [], "in_path": []}

    # yt-dlp 通常安装到的 bin 目录
    possible_bin_dirs = set()

    # 当前 Python 的 bin 目录
    py_bin = os.path.join(sys.prefix, "bin")
    possible_bin_dirs.add(py_bin)

    # pip 的 bin 目录
    for cmd in ["pip3", "pip"]:
        pip_path = shutil.which(cmd)
        if pip_path and os.path.islink(pip_path):
            pip_bin = os.path.dirname(os.path.realpath(pip_path))
            possible_bin_dirs.add(pip_bin)
        elif pip_path:
            pip_bin = os.path.dirname(pip_path)
            possible_bin_dirs.add(pip_bin)

    # 常见用户安装目录
    home = os.path.expanduser("~")
    is_windows = platform.system() == "Windows"

    if is_windows:
        possible_bin_dirs.add(os.path.join(home, "AppData", "Local", "Programs", "Python", f"Python{sys.version_info.major}{sys.version_info.minor}", "Scripts"))
        possible_bin_dirs.add(os.path.join(home, "AppData", "Roaming", "Python", f"Python{sys.version_info.major}{sys.version_info.minor}", "Scripts"))
        possible_bin_dirs.add(os.path.join(home, "AppData", "Local", "Programs", "Python", "Launcher"))
    else:
        possible_bin_dirs.add(os.path.join(home, ".local", "bin"))
        possible_bin_dirs.add(os.path.join(home, "Library", "Python", f"{sys.version_info.major}.{sys.version_info.minor}", "bin"))
        possible_bin_dirs.add("/usr/local/bin")
        possible_bin_dirs.add("/opt/homebrew/bin")

    # 针对 macOS .app bundle
    app_bin = os.path.join(sys.prefix, "..", "..", "bin")
    possible_bin_dirs.add(os.path.abspath(app_bin))

    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    for bin_dir in sorted(possible_bin_dirs):
        bin_dir = os.path.normpath(bin_dir)
        if not os.path.isdir(bin_dir):
            continue

        # 检查该目录下是否有 yt-dlp
        ytdlp_in_dir = os.path.join(bin_dir, "yt-dlp")
        has_ytdlp = os.path.exists(ytdlp_in_dir)
        # 也检查 yt-dlp.exe (Windows)
        if not has_ytdlp and is_windows:
            has_ytdlp = os.path.exists(ytdlp_in_dir + ".exe")

        in_path = any(os.path.normpath(d) == bin_dir for d in path_dirs)

        if has_ytdlp and not in_path:
            warn(f"yt-dlp 安装在 {bin_dir}，但该目录不在 PATH 中！")
            result["missing_from_path"].append(bin_dir)
        elif has_ytdlp and in_path:
            ok(f"{bin_dir} (在 PATH 中 ✓)")
            result["in_path"].append(bin_dir)
        elif in_path:
            dim(f"{bin_dir} (在 PATH 中，无 yt-dlp)")

    return result


def diagnose_shell_config() -> dict:
    """诊断 Shell 配置文件中的 PATH 设置"""
    section("7. Shell 配置检查")
    result = {"config_files_found": [], "relevant_lines": []}

    is_windows = platform.system() == "Windows"
    if is_windows:
        dim("  Windows 环境：跳过 Shell 配置文件检查")
        result["note"] = "Windows - skipped"
        return result

    home = os.path.expanduser("~")
    shell = os.environ.get("SHELL", "")

    config_files = []
    if "zsh" in shell:
        config_files = [
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".zprofile"),
            os.path.join(home, ".zshenv"),
        ]
    elif "bash" in shell:
        config_files = [
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".profile"),
        ]
    else:
        config_files = [
            os.path.join(home, ".profile"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".bashrc"),
        ]

    for cf in config_files:
        if os.path.exists(cf):
            result["config_files_found"].append(cf)
            dim(f"  检查: {cf}")

            try:
                with open(cf, 'r') as f:
                    for lineno, line in enumerate(f, 1):
                        stripped = line.strip()
                        # 忽略注释和空行
                        if not stripped or stripped.startswith('#'):
                            continue
                        # 找 PATH 相关行
                        if 'PATH' in stripped or 'path' in stripped.lower():
                            result["relevant_lines"].append(f"{cf}:{lineno}: {stripped}")
                            dim(f"    第{lineno}行: {stripped}")
            except Exception:
                pass

    return result


# ============================================================
# 聚合诊断
# ============================================================

def run_full_diagnosis() -> dict:
    """运行完整诊断，返回结构化结果"""
    print(f"\n{HEADER}{'=' * 60}")
    print(f"{HEADER}   yt-dlp 环境诊断工具 v1.0")
    print(f"{HEADER}{'=' * 60}{RESET}")
    print(f"  时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  系统: {platform.system()} {platform.release()}")

    results = {}

    results["system"]     = diagnose_system()
    results["pip"]         = diagnose_pip()
    results["ytdlp_cli"]   = diagnose_ytdlp_cli()
    results["ytdlp_module"]= diagnose_ytdlp_module()
    results["site_pkgs"]   = diagnose_site_packages()
    results["path_issue"]  = diagnose_path_issue()
    results["shell_config"]= diagnose_shell_config()

    return results


# ============================================================
# 自动修复
# ============================================================

def find_best_pip() -> str:
    """找到最匹配当前 Python 的 pip 命令"""
    current_py = f"{sys.version_info.major}.{sys.version_info.minor}"

    # 方案 1: 当前 Python 的 -m pip
    return f"\"{sys.executable}\" -m pip"


def fix_install_ytdlp(dry_run: bool = False) -> bool:
    """在当前 Python 环境中安装 yt-dlp"""
    section("🔧 自动修复: 安装 yt-dlp")

    pip_cmd = find_best_pip()
    install_cmd = f"{pip_cmd} install yt-dlp"

    info(f"执行命令: {install_cmd}")

    if dry_run:
        info(f"[DRY RUN] 将执行: {install_cmd}")
        return True

    print(f"\n  {WARN}即将执行安装，请确认...{RESET}")
    print(f"  命令: {install_cmd}")
    resp = input(f"  是否继续? [Y/n]: ").strip().lower()

    if resp and resp != 'y' and resp != 'yes':
        info("用户取消安装")
        return False

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "yt-dlp"],
            capture_output=False,
            text=True,
        )
        if result.returncode == 0:
            ok("yt-dlp 安装成功！")
            return True
        else:
            err(f"安装失败，返回码: {result.returncode}")
            return False
    except Exception as e:
        err(f"安装异常: {e}")
        return False


def fix_path_add(bin_dir: str, dry_run: bool = False) -> bool:
    """将目录添加到 PATH（通过修改 shell 配置文件）"""
    section("🔧 自动修复: 添加 PATH")

    is_windows = platform.system() == "Windows"

    if is_windows:
        info(f"将添加以下目录到用户 PATH:")
        info(f"  {bin_dir}")
        if dry_run:
            info("[DRY RUN] 跳过实际操作")
            return True

        print(f"\n  {WARN}Windows 下请手动添加 PATH:{RESET}")
        print(f"  1. Win+R → sysdm.cpl → 高级 → 环境变量")
        print(f"  2. 在用户变量 Path 中添加: {bin_dir}")
        print(f"  3. 重启终端生效")
        return False

    # macOS / Linux: 修改 shell 配置文件
    home = os.path.expanduser("~")
    shell = os.environ.get("SHELL", "")
    rc_file = os.path.join(home, ".zshrc" if "zsh" in shell else ".bashrc")

    export_line = f'\nexport PATH="{bin_dir}:$PATH"  # 由 diagnose_ytdlp.py 添加\n'

    info(f"将在 {rc_file} 末尾添加:")
    dim(f"  {export_line.strip()}")

    if dry_run:
        info("[DRY RUN] 跳过实际操作")
        return True

    resp = input(f"  是否添加? [Y/n]: ").strip().lower()
    if resp and resp != 'y' and resp != 'yes':
        info("用户取消")
        return False

    try:
        # 检查是否已有类似行
        if os.path.exists(rc_file):
            with open(rc_file, 'r') as f:
                content = f.read()
            if bin_dir in content:
                warn(f"{bin_dir} 已在 {rc_file} 中，跳过添加")
                return True

        with open(rc_file, 'a') as f:
            f.write(export_line)

        ok(f"已添加到 {rc_file}")
        info(f"请执行以下命令使其生效: source {rc_file}")
        return True

    except Exception as e:
        err(f"写入失败: {e}")
        return False


def auto_fix(results: dict, auto_yes: bool = False, dry_run: bool = False) -> None:
    """根据诊断结果自动修复"""
    section("🛠️  自动修复方案")

    ytdlp_module = results.get("ytdlp_module", {})
    ytdlp_cli = results.get("ytdlp_cli", {})
    path_issue = results.get("path_issue", {})

    fixes_applied = []

    # 问题 1: 模块和 CLI 都没有 → 安装
    if not ytdlp_module.get("module_found") and not ytdlp_cli.get("cli_found"):
        err("诊断: yt-dlp 未安装")
        info("修复方案: 在当前 Python 环境中安装 yt-dlp")

        if auto_yes or not dry_run:
            success = fix_install_ytdlp(dry_run=dry_run)
            if success:
                fixes_applied.append("yt-dlp 安装")
    else:
        ok("yt-dlp 已安装，跳过安装步骤")

    # 问题 2: CLI 找不到但模块存在 → PATH 问题
    if ytdlp_module.get("module_found") and not ytdlp_cli.get("cli_found"):
        err("诊断: yt-dlp 模块已安装，但 CLI 命令不可用")

        # 找到 yt-dlp 可执行文件的位置
        bin_dirs_to_add = path_issue.get("missing_from_path", [])

        if bin_dirs_to_add:
            for bin_dir in bin_dirs_to_add:
                info(f"修复方案: 添加 {bin_dir} 到 PATH")
                if auto_yes or not dry_run:
                    success = fix_path_add(bin_dir, dry_run=dry_run)
                    if success:
                        fixes_applied.append(f"添加 {bin_dir} 到 PATH")
        else:
            # 尝试猜测路径
            py_bin = os.path.join(sys.prefix, "bin")
            if os.path.isdir(py_bin):
                info(f"修复方案: 添加 Python bin 目录到 PATH: {py_bin}")
                if auto_yes or not dry_run:
                    success = fix_path_add(py_bin, dry_run=dry_run)
                    if success:
                        fixes_applied.append(f"添加 {py_bin} 到 PATH")

    # 问题 3: 模块在其他 Python 环境中 → 重新安装
    if not ytdlp_module.get("module_found") and ytdlp_module.get("other_python_path"):
        err("诊断: yt-dlp 安装在其他 Python 环境中")
        info(f"修复方案: 用当前 Python ({sys.executable}) 重新安装")
        if auto_yes or not dry_run:
            success = fix_install_ytdlp(dry_run=dry_run)
            if success:
                fixes_applied.append("在当前 Python 中重新安装 yt-dlp")

    # 总结
    print()
    if fixes_applied:
        ok(f"已应用 {len(fixes_applied)} 项修复:")
        for f in fixes_applied:
            ok(f"  ✓ {f}")
        print(f"\n  {WARN}⚠ 请重启终端或执行以下命令使更改生效:{RESET}")
        print(f"  {INFO}source ~/.zshrc  (或 source ~/.bashrc){RESET}")
    else:
        info("无需修复，或所有问题已手动处理")
        if ytdlp_module.get("module_found") and ytdlp_cli.get("cli_found"):
            ok("✓ yt-dlp 环境一切正常！")


# ============================================================
# 汇总报告
# ============================================================

def print_summary(results: dict):
    """打印汇总报告"""
    section("📊 诊断汇总")

    ytdlp_cli = results.get("ytdlp_cli", {})
    ytdlp_module = results.get("ytdlp_module", {})
    pip_info = results.get("pip", {})

    issues = []
    status_ok = []

    # 检查项
    if ytdlp_cli.get("cli_found"):
        status_ok.append("yt-dlp CLI 可用")
    else:
        issues.append("yt-dlp CLI 不可用")

    if ytdlp_module.get("module_found"):
        status_ok.append("yt-dlp Python 模块可导入")
    else:
        issues.append("yt-dlp Python 模块不可导入")

    if pip_info.get("mismatch"):
        issues.append("pip 与当前 Python 版本不一致")
    else:
        status_ok.append("pip 与 Python 版本一致")

    path_issue = results.get("path_issue", {})
    if path_issue.get("missing_from_path"):
        issues.append(f"yt-dlp 安装目录不在 PATH 中 ({len(path_issue['missing_from_path'])} 个)")
    else:
        status_ok.append("PATH 配置正常")

    # 输出
    print()
    if status_ok:
        print(f"  {OK}通过项 ({len(status_ok)}):{RESET}")
        for item in status_ok:
            ok(f"  {item}")

    if issues:
        print(f"\n  {ERROR}问题项 ({len(issues)}):{RESET}")
        for item in issues:
            err(f"  {item}")

    # 总体评估
    print()
    if not issues:
        print(f"  {OK}🎉 结论: yt-dlp 环境完全正常！{RESET}")
    elif len(issues) <= 2:
        print(f"  {WARN}⚠️ 结论: 存在 {len(issues)} 个小问题，建议运行 --fix 自动修复{RESET}")
    else:
        print(f"  {ERROR}❌ 结论: 存在 {len(issues)} 个问题，强烈建议运行 --fix 自动修复{RESET}")

    # 快速修复命令
    print(f"\n  {DIM}--- 快速修复命令 ---{RESET}")
    print(f"  {INFO}# 方案1: 用当前 Python 安装{RESET}")
    print(f"  {BOLD}{sys.executable} -m pip install yt-dlp{RESET}")
    print(f"  {INFO}# 方案2: 验证安装{RESET}")
    print(f"  {BOLD}which yt-dlp && yt-dlp --version{RESET}")
    print(f"  {INFO}# 方案3: 如果 PATH 问题，重新加载配置{RESET}")
    print(f"  {BOLD}source ~/.zshrc  # 或 source ~/.bashrc{RESET}")
    print()


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="yt-dlp 环境诊断与自动修复工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                  # 仅诊断，不修改任何文件
  %(prog)s --fix            # 诊断并交互式修复
  %(prog)s --fix --yes      # 诊断并自动确认所有修复
  %(prog)s --dry-run        # 预览修复方案，不实际执行
  %(prog)s --json           # 输出 JSON 格式的诊断结果
        """,
    )

    parser.add_argument("--fix", action="store_true",
                        help="自动修复检测到的问题")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="自动确认所有修复操作（无需交互）")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览修复方案，不实际执行")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 格式输出诊断结果")

    args = parser.parse_args()

    if args.json:
        # JSON 模式：静默诊断，仅输出 JSON
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            results = run_full_diagnosis()
        finally:
            sys.stdout = old_stdout

        # 清理不可序列化的数据
        output = {}
        for key, value in results.items():
            if isinstance(value, dict):
                output[key] = {
                    k: v for k, v in value.items()
                    if isinstance(v, (str, int, float, bool, list, dict, type(None)))
                }
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        return

    # 标准模式
    results = run_full_diagnosis()

    print_summary(results)

    if args.fix:
        auto_fix(results, auto_yes=args.yes, dry_run=args.dry_run)
    elif args.dry_run:
        print(f"\n  {INFO}--dry-run: 预览修复方案（实际不会修改任何文件）{RESET}")
        auto_fix(results, auto_yes=True, dry_run=True)
    else:
        print(f"  {DIM}提示: 运行 --fix 可自动修复检测到的问题{RESET}")
        print(f"  {DIM}      python3 diagnose_ytdlp.py --fix{RESET}")

    print()


if __name__ == "__main__":
    main()
