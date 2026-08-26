# -*- coding: utf-8 -*-
"""主题与样式 (theme) — 纯 GUI 工具。

包含深色主题 QSS、Windows 11 云母背景支持、阶段切换提示音等。
这些工具与业务逻辑无关，只负责视觉和音效呈现。
"""

import ctypes
import io
import math
import os
import struct
import sys
import tempfile
import wave

try:
    import winsound  # Windows 标准库：播放提示音
    HAS_WINSOUND = True
except ImportError:
    winsound = None
    HAS_WINSOUND = False

from PySide6.QtWidgets import QApplication


# ============================================================================
#  常量
# ============================================================================

# 深色主题下更亮的阶段配色
DARK_PHASE_COLORS = {"work": "#ff6b6b", "short_break": "#2ecc71",
                     "long_break": "#5dade2", "restart": "#f39c12"}

# 主窗口显示模式尺寸
COMPACT_W, COMPACT_H = 400, 240          # 收起模式尺寸
EXPANDED_MIN_W, EXPANDED_MIN_H = 400, 900  # 展开模式最小尺寸
TOP_RIGHT_MARGIN = 24                    # 自动收起时距屏幕右上角的距离(px)


# ============================================================================
#  深色主题 QSS
# ============================================================================

QSS = """
QMainWindow, QWidget#central { background: #1e1e1e; }
QGroupBox {
    font-weight: bold;
    border: 1px solid #3c3c3c;
    border-radius: 6px;
    margin-top: 10px;
    padding: 8px 6px 6px 6px;
    background: #252526;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #d4d4d4; }
QLabel { color: #e8e8e8; }
QCheckBox { color: #e8e8e8; spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; }
QPushButton {
    padding: 5px 14px;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    background: #333333;
    color: #e8e8e8;
}
QPushButton:hover { background: #3e3e3e; }
QPushButton:pressed { background: #2a2a2a; }
QPushButton:disabled { color: #6a6a6a; background: #2a2a2a; }
QLineEdit, QListWidget, QPlainTextEdit, QSpinBox, QDoubleSpinBox {
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    padding: 3px;
    background: #1e1e1e;
    color: #e8e8e8;
    selection-background-color: #264f78;
}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    background: #333333; border: none; width: 16px;
}
QListWidget::item { padding: 2px; }
QListWidget::item:selected { background: #094771; color: #ffffff; }
QPlainTextEdit { font-family: Consolas; font-size: 9pt; }
QToolTip { background: #333333; color: #e8e8e8; border: 1px solid #3c3c3c; }
QMenu { background: #2d2d30; color: #e8e8e8; border: 1px solid #3c3c3c; }
QMenu::item { padding: 5px 24px 5px 12px; }
QMenu::item:selected { background: #094771; color: #ffffff; }
QMenu::item:disabled { color: #6a6a6a; }
QMenu::separator { height: 1px; background: #3c3c3c; margin: 4px 8px; }
QMessageBox { background: #252526; }
QMessageBox QLabel { color: #e8e8e8; }
QTabWidget::pane { border: 1px solid #3c3c3c; border-radius: 6px; background: #252526; }
QTabBar::tab {
    background: #333333; color: #e8e8e8; padding: 6px 20px;
    border: 1px solid #3c3c3c; border-bottom: none;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QTabBar::tab:selected { background: #094771; color: #ffffff; }
QTabBar::tab:hover:!selected { background: #3e3e3e; }
"""

# 云母模式下：主背景透明以透出系统云母，分组框半透明保持内容可读
QSS_MICA = QSS.replace(
    "QMainWindow, QWidget#central { background: #1e1e1e; }",
    "QMainWindow, QWidget#central { background: transparent; }",
).replace(
    "background: #252526;",
    "background: rgba(37, 37, 38, 205);",
)


# ============================================================================
#  Windows 11 云母 (Mica) 背景
# ============================================================================

# DWM 常量：Windows 11 22H2(22621)+ 才支持
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_MAINWINDOW = 2  # Mica


def supports_mica():
    """判断系统是否为 Windows 11 22H2(22621) 及以上。"""
    try:
        v = sys.getwindowsversion()
        return (v.major, v.minor, v.build) >= (10, 0, 22621)
    except Exception:
        return False


def apply_mica(hwnd, dark=True):
    """给窗口启用 Mica（完整序列，与 win32mica 库一致）：

    1) DwmExtendFrameIntoClientArea(-1) —— 把边框扩展到整个客户区，
       这是 Mica 能铺满窗口背景的关键（只设 DWM 属性不会渲染）。
    2) SetWindowCompositionAttribute(AccentState=5=ACCENT_ENABLE_HOSTBACKDROP)
    3) DwmSetWindowAttribute(20) 深色标题栏（与背景同材质，视觉一致）
    4) DwmSetWindowAttribute(38) 背景材质 = Mica
    """
    if os.name != "nt" or not hwnd:
        return False

    class _MARGINS(ctypes.Structure):
        _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

    class _AccentPolicy(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_uint), ("AccentFlags", ctypes.c_uint),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_uint)]

    class _WCA(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int),
                    ("Data", ctypes.POINTER(ctypes.c_int)),
                    ("SizeOfData", ctypes.c_size_t)]

    try:
        user32 = ctypes.windll.user32
        dwm = ctypes.windll.dwmapi

        # 1) 边框扩展到整个客户区（关键步骤）
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(_MARGINS(-1, -1, -1, -1)))

        # 2) 宿主背景：AccentState=5 = ACCENT_ENABLE_HOSTBACKDROP
        acp = _AccentPolicy()
        acp.AccentState = 5
        acp.AccentFlags = 0
        acp.GradientColor = int("00cccccc", base=16)
        acp.AnimationId = 0
        wca = _WCA()
        wca.Attribute = 20  # WCA_ACCENT_POLICY
        wca.SizeOfData = ctypes.sizeof(acp)
        wca.Data = ctypes.cast(ctypes.pointer(acp), ctypes.POINTER(ctypes.c_int))
        try:
            user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(wca))
        except Exception:
            pass

        # 3) 深色标题栏
        dark_val = ctypes.c_int(1 if dark else 0)
        r1 = dwm.DwmSetWindowAttribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                       ctypes.byref(dark_val), ctypes.sizeof(dark_val))
        # 4) 背景材质 = Mica
        backdrop = ctypes.c_int(_DWMSBT_MAINWINDOW)
        r2 = dwm.DwmSetWindowAttribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                                       ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        return r1 == 0 and r2 == 0
    except Exception:
        return False


# ============================================================================
#  状态切换提示音
# ============================================================================

# 说明：Python 3.13 的 winsound 禁止 SND_ASYNC+SND_MEMORY（RuntimeError），
#       因此把合成好的 WAV 写入系统临时目录，再用 SND_FILENAME|SND_ASYNC 播放。

_CHIME_DIR = os.path.join(tempfile.gettempdir(), "pomodoro_guard_chimes")
_CHIME_FREQS = {
    "work": [587.33, 880.00],          # 上行双音：开始工作
    "short_break": [880.00, 587.33],   # 下行双音：短休息
    "long_break": [523.25, 659.25, 783.99],  # 三连音：长休息
    "restart": [440.00, 392.00, 440.00],  # 警示三连：重启阶段（SOS）
}


def _make_chime_wav(freqs, dur=0.22, sr=22050, volume=0.4):
    """把若干频率串成一段带淡入淡出的提示音 WAV 字节。"""
    frames = bytearray()
    for f in freqs:
        n = int(sr * dur)
        for j in range(n):
            env = min(1.0, j / 60.0, (n - j) / 60.0)  # 淡入淡出防爆音
            frames += struct.pack("<h",
                                  int(32767 * volume * env * math.sin(2 * math.pi * f * j / sr)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _ensure_chime_files():
    """把提示音 WAV 写入临时目录（只生成一次），返回 阶段 -> 文件路径。"""
    paths = {}
    try:
        os.makedirs(_CHIME_DIR, exist_ok=True)
    except OSError:
        return paths
    for phase, freqs in _CHIME_FREQS.items():
        path = os.path.join(_CHIME_DIR, f"{phase}.wav")
        try:
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(_make_chime_wav(freqs))
            paths[phase] = path
        except OSError:
            continue
    return paths


# 模块加载时生成提示音文件
_CHIMES = _ensure_chime_files()


def play_state_sound(phase, enabled=True):
    """播放阶段切换提示音；无 winsound 或播放失败时静默（绝不打断程序）。"""
    if not enabled:
        return
    path = _CHIMES.get(phase)
    if HAS_WINSOUND and path:
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        except Exception:
            pass
    # 兜底：系统蜂鸣（须已存在 QApplication 实例，否则会段错误）
    try:
        if QApplication.instance() is not None:
            QApplication.beep()
    except Exception:
        pass
