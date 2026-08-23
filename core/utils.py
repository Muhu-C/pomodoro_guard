# -*- coding: utf-8 -*-
"""工具函数：进程名规范化、子进程输出解码，以及共享常量与 psutil 探测。

供 core/ 下各模块复用；psutil 未安装时置为 None，调用方需用 HAS_PSUTIL 判断回退。
"""

import locale
import os

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # psutil 未安装时使用 tasklist / taskkill 回退
    psutil = None
    HAS_PSUTIL = False

# 阶段显示名与配色（供界面层展示用）
PHASE_NAMES = {"work": "工作", "short_break": "短休息", "long_break": "长休息"}
PHASE_COLORS = {"work": "#e74c3c", "short_break": "#27ae60", "long_break": "#2980b9"}

# 不弹出黑窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def normalize_exe(name):
    """把用户输入规范化为 exe 名：去掉路径/引号/通配符，补上 .exe。

    注意：进程名可能本身含点（如商店版 python 的进程名 pythonw3.13），
    因此只按是否以 .exe 结尾来判断，而不是按是否含点。
    输入为空/纯空白时返回空字符串，调用方须据此拒绝添加。
    """
    name = (name or "").strip().strip('"').strip("*").strip()
    name = os.path.basename(name)
    if not name:
        return ""
    if not name.lower().endswith(".exe"):
        name += ".exe"
    return name.lower()


def proc_name_of(exe_name):
    """从规范化 exe 名得到进程名（去掉结尾的 .exe，供 Get-Process 使用）。"""
    exe_name = normalize_exe(exe_name)
    return exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name


def decode_bytes(raw):
    """按系统代码页解码子进程输出（兼容中文 Windows 的 GBK 与 UTF-8 系统）。

    中文系统上 taskkill / powershell 的输出是 GBK(CP936)，直接按 UTF-8
    解会得到乱码；这里先试 UTF-8，失败再回退到系统首选编码。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode(locale.getpreferredencoding(False))
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")