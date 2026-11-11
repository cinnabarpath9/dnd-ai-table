# coding=utf-8
"""打「网盘包」：把玩家拿到就能跑的那一份，压成 zip。

和在仓库里开发的区别只有三点：

1. 不带 `.git`、`__pycache__`、测试产物；
2. 多几个启动脚本（Windows 双击 / macOS-Linux 终端 / Termux）；
3. 多一份 `说明.txt`，写给「只下载、不读源码」的人。

用法：

    python tools/build_package.py pc       # 电脑端（Windows / macOS / Linux）
    python tools/build_package.py phone    # 手机端（Android + Termux）
    python tools/build_package.py both

产物落在 `dist/`，命名与同系列的狼人杀一致：
`dnd-ai-table-<版本>-pc-desktop.zip` / `-phone-termux.zip`。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 包里要带的东西。分析目录只留图（README 引用）与两个成本脚本。
INCLUDE_DIRS = ("engine", "adventures", "docs", "tests", "tools", "analysis")
INCLUDE_FILES = ("README.md", "LICENSE", "NOTICE", "VERSION", ".gitignore")
#: 分析目录里不带的东西（这些是过程记录，不是使用者需要的东西）
SKIP_EXT = {".state", ".json"}          # analysis/*.state.json
SKIP_NAMES = {"_last_report.json"}
SKIP_DIRS = {"__pycache__", ".git", "_logs", "_profiles"}

FLAVORS = ("pc", "phone")
FLAVOR_ALIAS = {"pc": "pc-desktop", "phone": "phone-termux"}

README_PC = """D&D AI 跑团 · 电脑端
==============================

【这是什么】
一个跑在你自己电脑上的单机 AI 跑团：你坐一个座位，DM 和队友由大模型扮演。
骰子、命中、伤害、豁免全部由规则引擎算，模型只负责写叙事。

【需要什么】
· Python 3.11 或更新版本（3.14 上测过）
· 不需要装任何第三方库：整个项目是纯标准库
· 不需要联网：假模型路径全程离线，一分钱不花

【怎么跑】
Windows：双击「启动-Windows.bat」
macOS / Linux：终端里执行  bash 启动-macOS-Linux.sh

跑起来后浏览器会自动打开 http://127.0.0.1:8770/
没有自动打开的话，把上面这个地址手动粘进浏览器。

【想接真模型（花自己的钱，默认关闭）】
1. 把 tools/seat-config.example.json 复制到本目录外面，改成自己的座位；
2. key 放环境变量（例如 QIANFAN_API_KEY），不要写进任何文件；
3. 启动时加 --real 和调用上限：
   python -m engine.demo_web --session-zero --real --seat-config ..\\我的座位.json --max-calls 40
细节见 README.md 的「快速开始」。

【存档与复盘】
存档在 saves\\，第一次运行自动创建；网页里的「复盘」可以回放整局。

【出问题了】
· 提示找不到 Python：装一个 3.11+，安装时勾上 "Add python.exe to PATH"。
· 端口被占用：加 --port 8771 换一个。
· 页面打不开：看窗口里打印的地址，确认服务还在跑。

【许可】
源码可见 + 有限授权：可以自己玩、私下改；不可商用、不可再分发。
完整条款见 LICENSE。SRD 5.2 数据是 CC-BY-4.0，署名见 NOTICE。

【反馈 / 支持】
Q 群：https://qm.qq.com/q/68VcRJr3Py
爱发电：https://afdian.com/a/langrensha
同系列（AI 狼人杀）：https://github.com/cinnabarpath9/werewolf
"""

README_PHONE = """D&D AI 跑团 · 手机端（Android + Termux）
==============================

【先说清楚】
服务只绑 127.0.0.1（手机自己），别的设备连不进来。
所以这是「把服务跑在手机里、用手机自己的浏览器玩」，不是局域网联机。
要局域网/多设备，得改绑定地址（自己改代码，安全自负）。

【需要什么】
· Android 手机
· Termux：从 F-Droid 或 GitHub 装，不要用应用商店那些停更的老版本

【怎么跑】
1. 打开 Termux，装依赖（只需一次）：
     pkg update -y && pkg install -y python
2. 把这个目录放到手机上，例如：
     ~/dnd-ai-table
3. 进目录并启动：
     cd ~/dnd-ai-table && bash 启动-Termux.sh
4. 手机浏览器打开：http://127.0.0.1:8770/

【想接真模型】
同上，用 --real + --seat-config；key 放环境变量：
     export QIANFAN_API_KEY=你的key
注意手机端跑真模型会更慢，也更耗电。

【存档】
saves/ 目录，在 Termux 的 home 下。

【许可】
源码可见 + 有限授权：可以自己玩、私下改；不可商用、不可再分发。
SRD 5.2 数据 CC-BY-4.0，见 NOTICE。

【反馈】
Q 群：https://qm.qq.com/q/68VcRJr3Py
"""

LAUNCH_WIN = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   D&D AI 跑团 · 本地开局
echo   浏览器会自动打开 http://127.0.0.1:8770/
echo   关掉这个窗口 = 结束游戏
echo ============================================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo [找不到 Python]
  echo 请先安装 Python 3.11+ : https://www.python.org/downloads/
  echo 安装时务必勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)
python -m engine.demo_web --session-zero --open
echo.
echo 服务已结束。按任意键关闭窗口。
pause >nul
"""

LAUNCH_POSIX = r"""#!/usr/bin/env bash
# macOS / Linux 启动脚本
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo "找不到 Python 3.11+。请先安装：https://www.python.org/downloads/"
  exit 1
fi

echo "使用 $PY 启动；浏览器会自动打开 http://127.0.0.1:8770/"
exec "$PY" -m engine.demo_web --session-zero --open
"""

LAUNCH_TERMUX = r"""#!/data/data/com.termux/files/usr/bin/bash
# Android Termux 启动脚本
cd "$(dirname "$0")"

if ! command -v python >/dev/null 2>&1; then
  echo "第一次运行：正在安装 Python（需要网络，几分钟）……"
  pkg update -y && pkg install -y python
fi

echo "启动后，用手机浏览器打开： http://127.0.0.1:8770/"
exec python -m engine.demo_web --session-zero
"""


def version() -> str:
    p = os.path.join(ROOT, "VERSION")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            v = fh.read().strip()
        if v:
            return v
    return "0.0.0"


def copy_tree(src: str, dst: str) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = os.path.relpath(root, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for name in files:
            if name in SKIP_NAMES:
                continue
            if os.path.splitext(name)[1] in SKIP_EXT and "analysis" in root:
                continue
            shutil.copy2(os.path.join(root, name), os.path.join(target, name))


def write(path: str, text: str, newline: str = "\n", bom: bool = False) -> None:
    """写文本。说明文件带 BOM（老记事本才认得出 UTF-8）；启动脚本不带。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    enc = "utf-8-sig" if bom else "utf-8"
    with open(path, "w", encoding=enc, newline=newline) as fh:
        fh.write(text)


def stage(flavor: str, ver: str) -> str:
    stage_root = tempfile.mkdtemp(prefix=f"pkg-{flavor}-")
    name = f"dnd-ai-table-{ver}-{FLAVOR_ALIAS[flavor]}"
    out = os.path.join(stage_root, name)
    os.makedirs(out, exist_ok=True)

    for d in INCLUDE_DIRS:
        src = os.path.join(ROOT, d)
        if os.path.isdir(src):
            copy_tree(src, os.path.join(out, d))
    for f in INCLUDE_FILES:
        src = os.path.join(ROOT, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out, f))

    if flavor == "pc":
        write(os.path.join(out, "启动-Windows.bat"), LAUNCH_WIN, newline="\r\n")
        write(os.path.join(out, "启动-macOS-Linux.sh"), LAUNCH_POSIX)
        write(os.path.join(out, "说明.txt"), README_PC, newline="\r\n", bom=True)
    else:
        write(os.path.join(out, "启动-Termux.sh"), LAUNCH_TERMUX)
        write(os.path.join(out, "说明.txt"), README_PHONE, bom=True)

    for sh in ("启动-macOS-Linux.sh", "启动-Termux.sh"):
        p = os.path.join(out, sh)
        if os.path.isfile(p):
            os.chmod(p, 0o755)
    return out


def zip_dir(src: str, dst: str) -> tuple[int, str]:
    n = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        base = os.path.dirname(src)
        for root, dirs, files in os.walk(src):
            dirs.sort()
            for name in sorted(files):
                p = os.path.join(root, name)
                zf.write(p, os.path.relpath(p, base))
                n += 1
    h = hashlib.sha256(open(dst, "rb").read()).hexdigest()
    return n, h


def build(flavor: str) -> int:
    ver = version()
    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    stage_dir = stage(flavor, ver)
    name = os.path.basename(stage_dir)
    dst = os.path.join(dist, f"{name}.zip")
    n, sha = zip_dir(stage_dir, dst)
    shutil.rmtree(os.path.dirname(stage_dir), ignore_errors=True)
    size_mb = os.path.getsize(dst) / 1024 / 1024
    print(f"[OK] {os.path.relpath(dst, ROOT)}  {size_mb:.2f} MB  {n} 个文件")
    print(f"     sha256 {sha}")
    return 0


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "both").lower()
    flavors = FLAVORS if arg in ("both", "all", "") else (arg,)
    for f in flavors:
        if f not in FLAVORS:
            print(f"未知 flavor：{f}（可选 pc / phone / both）")
            return 2
        build(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())