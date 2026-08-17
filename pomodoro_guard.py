# -*- coding: utf-8 -*-
"""
番茄钟 · 应用监管 —— PySide6 界面层
====================================
依赖 PySide6（pip install PySide6）。核心逻辑在 pomodoro_core.py。

运行：python pomodoro_guard.py
"""

import ctypes
import io
import math
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

try:
    import winsound  # Windows 标准库：播放提示音
    HAS_WINSOUND = True
except ImportError:
    winsound = None
    HAS_WINSOUND = False

from PySide6.QtCore import QEvent, QLockFile, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QIcon, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDoubleSpinBox, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSystemTrayIcon,
    QTabWidget, QToolTip, QVBoxLayout, QWidget,
)

from pomodoro_core import (
    CONFIG_PATH, PHASE_NAMES, ProcessManager, PomodoroEngine,
    load_config, normalize_exe, save_config,
)
from presence import PresenceDetector
from stats import FocusStats

APP_TITLE = "番茄钟 · 应用监管"

# 单例锁：位于系统临时目录（避免脚本目录只读导致锁创建失败）
LOCK_PATH = os.path.join(tempfile.gettempdir(), "pomodoro_guard_qt.lock")

# 主窗口显示模式：收起 = 仅番茄钟+功能按钮；展开 = 含全部设置配置
COMPACT_W, COMPACT_H = 400, 240          # 收起模式尺寸
EXPANDED_MIN_W, EXPANDED_MIN_H = 400, 900  # 展开模式最小尺寸（与默认窗口一致）
TOP_RIGHT_MARGIN = 24                    # 自动收起时距屏幕右上角的距离(px)

# 摄像头在场检测：休息阶段人离开超过该秒数则关闭遮罩（固定值，不提供配置）
REST_AWAY_SECONDS = 60.0


def acquire_lock():
    """尝试获取单例锁。

    返回持有锁的 QLockFile 对象（调用方须保持引用直到程序结束），
    若已有实例在运行则返回 None。崩溃残留的陈旧锁会自动清除。
    """
    lock = QLockFile(LOCK_PATH)
    lock.removeStaleLockFile()  # 上次异常退出残留的锁会被清掉
    if lock.tryLock(100):
        return lock
    return None

# 深色主题下更亮的阶段配色（浅色主题配色见 pomodoro_core.PHASE_COLORS）
DARK_PHASE_COLORS = {"work": "#ff6b6b", "short_break": "#2ecc71",
                     "long_break": "#5dade2"}

# 引擎状态 → 界面文案（状态栏/托盘菜单共用）
STATE_NAMES = {"running": "进行中", "paused": "已暂停", "idle": "待开始"}

# 完整深色主题：所有前景/背景色均显式指定，不受系统深浅色主题影响
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


def make_tray_icon():
    """用 QPainter 画一个番茄图标（无需外部图片文件）。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    # 番茄身体
    p.setBrush(QColor("#e74c3c"))
    p.drawEllipse(10, 18, 44, 40)
    # 顶部叶子
    p.setBrush(QColor("#2ecc71"))
    p.drawEllipse(16, 6, 16, 14)
    p.drawEllipse(34, 6, 16, 14)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------------------
# 状态切换提示音
# 说明：Python 3.13 的 winsound 禁止 SND_ASYNC+SND_MEMORY（RuntimeError），
#       因此把合成好的 WAV 写入系统临时目录，再用 SND_FILENAME|SND_ASYNC 播放。
# ---------------------------------------------------------------------------
_CHIME_DIR = os.path.join(tempfile.gettempdir(), "pomodoro_guard_chimes")
_CHIME_FREQS = {
    "work": [587.33, 880.00],          # 上行双音：开始工作
    "short_break": [880.00, 587.33],   # 下行双音：短休息
    "long_break": [523.25, 659.25, 783.99],  # 三连音：长休息
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


# ---------------------------------------------------------------------------
# Windows 11 云母 (Mica) 背景
# ---------------------------------------------------------------------------
# DWM 常量：Windows 11 22H2(22621)+ 才支持
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_MAINWINDOW = 2  # Mica


def _supports_mica():
    """判断系统是否为 Windows 11 22H2(22621) 及以上。"""
    try:
        v = sys.getwindowsversion()
        return (v.major, v.minor, v.build) >= (10, 0, 22621)
    except Exception:
        return False


def _apply_mica(hwnd, dark=True):
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


# 云母模式下：主背景透明以透出系统云母，分组框半透明保持内容可读
QSS_MICA = QSS.replace(
    "QMainWindow, QWidget#central { background: #1e1e1e; }",
    "QMainWindow, QWidget#central { background: transparent; }",
).replace(
    "background: #252526;",
    "background: rgba(37, 37, 38, 205);",
)


# ---------------------------------------------------------------------------
# 休息全屏遮罩
# 进入休息阶段时强制全屏覆盖（仅本软件主窗口悬浮其上），提醒放松双眼。
# ---------------------------------------------------------------------------
REST_OVERLAY_TEXT = "请立即看向6米以外的窗外物体，并眨眼10次！"


class RestOverlay(QWidget):
    """全屏强制遮罩：无边框、置顶、覆盖所有屏幕、拦截鼠标。

    中央显示护眼提醒文本；不接收焦点（键盘焦点留在主窗口），且禁止被关闭
    （closeEvent 忽略），只能由主窗口按阶段切换显示/隐藏。
    """

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("休息提醒")
        self._build()

    def _build(self):
        vg = self._virtual_geometry()
        self.setGeometry(vg)
        self.setStyleSheet("background: rgba(10, 10, 14, 235);")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 40, 40, 40)

        # 中央大字护眼提醒，字号按屏幕宽度自适应
        label = QLabel(REST_OVERLAY_TEXT)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        px = max(28, min(56, int(vg.width() / 18)))
        label.setStyleSheet(
            f"color: #f5f6fa; font-size: {px}px; font-weight: bold; "
            "background: transparent;")
        lay.addStretch(1)
        lay.addWidget(label)
        lay.addStretch(1)

        hint = QLabel("休息阶段 · 请放松双眼\n可通过主窗口或托盘菜单提前「跳过」休息")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            "color: #8a8a8a; font-size: 13px; background: transparent;")
        lay.addWidget(hint)

    @staticmethod
    def _virtual_geometry():
        """所有屏幕的联合矩形（多显示器全覆盖）。"""
        screens = QApplication.screens()
        if not screens:
            return QRect(0, 0, 1920, 1080)
        rect = screens[0].virtualGeometry()
        for s in screens[1:]:
            rect = rect.united(s.virtualGeometry())
        return rect

    def show_overlay(self):
        """全屏显示并置顶；不激活、不抢键盘焦点。"""
        self.setGeometry(self._virtual_geometry())
        self.show()
        self.raise_()

    def keyPressEvent(self, event):
        # 遮罩不响应任何按键（防 Esc 等绕过）
        event.ignore()

    def mousePressEvent(self, event):
        # 拦截点击，防止穿透到下层程序（"强制"遮罩）
        event.accept()

    def closeEvent(self, event):
        # 禁止被关闭（Alt+F4 / 系统关闭均忽略），只能由程序按阶段切换隐藏
        event.ignore()


# ---------------------------------------------------------------------------
# 数据统计：柱状图组件 + 统计面板
# ---------------------------------------------------------------------------
def format_duration(sec):
    """秒 → "X 小时 Y 分"（不足 1 小时只显示分钟）。"""
    sec = max(0, int(sec))
    h, m = divmod(sec // 60, 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    if h:
        return f"{h} 小时"
    return f"{m} 分钟"


class BarChart(QWidget):
    """轻量自绘柱状图：items=[(标签, 秒), ...]，柱顶标注 + 鼠标悬停显示精确值。"""

    BAR_COLOR = "#5dade2"
    BAR_HOVER = "#7fb3e8"
    GRID = "#3c3c3c"
    AXIS_TEXT = "#b0b0b0"
    VAL_TEXT = "#e8e8e8"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.items = []      # [(label, value_sec), ...]
        self._hover = -1
        self.setMinimumHeight(150)

    def set_data(self, items):
        self.items = list(items)
        self._hover = -1
        self.update()

    @staticmethod
    def _fmt(sec):
        h, m = divmod(int(sec) // 60, 60)
        if h and m:
            return f"{h}h{m}m"
        if h:
            return f"{h}h"
        return f"{m}m"

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if not self.items:
            return
        left, right, top, bottom = 36, 8, 16, 26
        plot_w, plot_h = w - left - right, h - top - bottom
        max_v = max((v for _, v in self.items), default=0)
        if max_v <= 0 or plot_h <= 10:
            return
        max_hours = max(1.0, max_v / 3600.0)
        # 横纵网格（小时刻度）
        n_grid = max(1, int(max_hours))
        for g in range(0, n_grid + 1):
            y = top + plot_h - int(plot_h * g / n_grid)
            p.setPen(QPen(QColor(self.GRID), 1))
            p.drawLine(left, y, w - right, y)
            p.setPen(QColor(self.AXIS_TEXT))
            p.drawText(0, y - 6, left - 6, 12, Qt.AlignRight | Qt.AlignVCenter,
                       f"{g}h")
        # 柱子
        n = len(self.items)
        slot = plot_w / n
        bar_w = min(slot * 0.62, 60.0)
        for i, (label, val) in enumerate(self.items):
            x = left + slot * i + (slot - bar_w) / 2
            ratio = (val / 3600.0) / max_hours
            bh = max(1, int(plot_h * ratio))
            y = top + plot_h - bh
            color = QColor(self.BAR_HOVER if i == self._hover else self.BAR_COLOR)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, y, bar_w, bh), 3, 3)
            if bh > 20:
                p.setPen(QColor(self.VAL_TEXT))
                p.drawText(QRectF(x - 8, y - 16, bar_w + 16, 14),
                           Qt.AlignCenter, self._fmt(val))
            p.setPen(QColor(self.AXIS_TEXT))
            p.drawText(QRectF(x - 14, h - bottom + 4, bar_w + 28, 16),
                       Qt.AlignCenter, label)

    def _hit(self, pos_x):
        if not self.items:
            return -1
        slot = self.width() / len(self.items)
        idx = int((pos_x - 36) // slot)
        return idx if 0 <= idx < len(self.items) else -1

    def mouseMoveEvent(self, event):
        idx = self._hit(event.position().x())
        if idx != self._hover:
            self._hover = idx
            self.update()
        if idx >= 0:
            label, val = self.items[idx]
            h, m = divmod(int(val) // 60, 60)
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"{label}  {h}小时{m}分", self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        QToolTip.hideText()
        super().leaveEvent(event)


class StatsPanel(QWidget):
    """「统计」页：时间档切换 + 专注总时长大数字 + streak + 柱状图 + 清除。"""

    PERIODS = [("today", "今日"), ("week", "本周"), ("month", "本月"),
               ("total", "累计")]

    def __init__(self, stats, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._period = "today"
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        # 时间档按钮（互斥）
        row = QHBoxLayout()
        self._btns = {}
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for key, text in self.PERIODS:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setMinimumHeight(26)
            b.clicked.connect(lambda _=False, k=key: self._set_period(k))
            self._btns[key] = b
            grp.addButton(b)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._btns["today"].setChecked(True)
        # 大数字
        self.lbl_big = QLabel("0 分钟")
        self.lbl_big.setAlignment(Qt.AlignCenter)
        self.lbl_big.setFont(QFont("Consolas", 32, QFont.Bold))
        self.lbl_big.setStyleSheet("color: #f5f6fa; background: transparent;")
        v.addWidget(self.lbl_big)
        # streak
        self.lbl_streak = QLabel("")
        self.lbl_streak.setAlignment(Qt.AlignCenter)
        self.lbl_streak.setStyleSheet("color: #9d9d9d; background: transparent;")
        v.addWidget(self.lbl_streak)
        # 柱状图 / 空状态占位（二选一显示）
        self.chart = BarChart()
        self.lbl_empty = QLabel("")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        self.lbl_empty.setStyleSheet(
            "color: #6a6a6a; background: transparent; font-size: 12px;")
        v.addWidget(self.chart, 1)
        v.addWidget(self.lbl_empty, 1)
        self.chart.hide()
        self.lbl_empty.hide()
        # 清除
        btn_clear = QPushButton("清除历史数据")
        btn_clear.setMinimumHeight(26)
        btn_clear.clicked.connect(self._on_clear)
        v.addWidget(btn_clear)

    def _set_period(self, key):
        self._period = key
        self._btns[key].setChecked(True)
        self.refresh(0)  # 档切换重绘

    def refresh(self, live_extra=0):
        """刷新面板；live_extra=当前工作阶段已运行秒数（仅今日档叠加）。"""
        st = self._stats
        if self._period == "today":
            total = st.today_duration() + max(0, int(live_extra))
            self.lbl_big.setText(format_duration(total))
            self.chart.hide()
            self.lbl_empty.hide()
        elif self._period == "week":
            items = [(k[5:], v) for k, v in st.daily_totals(7).items()]
            self._render_chart(items, "本周暂无专注记录")
        elif self._period == "month":
            items = [(k[5:], v) for k, v in st.daily_totals(30).items()]
            self._render_chart(items, "本月暂无专注记录")
        else:  # total
            items = st.monthly_totals()
            self._render_chart(items, "暂无专注记录")
        n = st.streak()
        self.lbl_streak.setText(
            f"已连续 {n} 天专注" if n > 0 else "今日尚未完成番茄")

    def _render_chart(self, items, empty_text):
        total = sum(v for _, v in items)
        self.lbl_big.setText(format_duration(total))
        has = any(v > 0 for _, v in items)
        self.chart.setVisible(has)
        self.lbl_empty.setVisible(not has)
        if has:
            self.chart.set_data(items)
        else:
            self.lbl_empty.setText(empty_text)

    def _on_clear(self):
        if QMessageBox.question(
                self, "清除历史数据",
                "将删除全部统计记录，不可恢复。确定？") != QMessageBox.Yes:
            return
        self._stats.clear()
        self.refresh(0)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        # 最小宽度 = 启动宽度；高度适当增大以容纳逐行布局
        self.resize(400, 900)
        self.setMinimumWidth(400)
        self.setMinimumHeight(890)

        self.pm = ProcessManager()
        self.engine = PomodoroEngine()
        self.cfg = load_config()
        self.monitored = []              # 监管的应用名列表
        self._scan_busy = False          # 后台扫描进行中标记
        self._log_queue = queue.Queue()  # 后台线程 -> 主线程日志队列
        self._fail_cooldown = {}         # exe -> 下次重试时间(monotonic)
        self._fail_msg = {}              # exe -> 上次失败详情
        self._last_scan = 0.0            # 上次扫描时间戳(monotonic)
        self._mica_supported = _supports_mica()
        self._mica_applied = False
        self._mica_logged = False
        self._overlay = None          # 休息全屏遮罩（首次进入休息时创建）
        self._topmost_forced = False  # 遮罩期间临时强制主窗口置顶（用户未勾选时）
        self._compact = False         # 当前是否收起模式（构造后立即切为收起）
        self._expanded_size = None    # 展开模式的窗口尺寸（收起时记录）
        self._presence = None         # 摄像头在场检测器（首次需要时创建）
        self._presence_paused = False  # 是否因"离开"自动暂停（可被自动恢复）
        self._presence_err_logged = False  # 摄像头不可用是否已记录日志
        self._stats = FocusStats()    # 专注时长统计（SQLite）

        self._build_ui()
        self._load_config_to_ui()

        backend_names = {"psutil": "psutil", "powershell": "PowerShell",
                         "tasklist": "tasklist/taskkill (内置)"}
        self.log(f"程序启动，检测后端: {backend_names.get(self.pm.backend, self.pm.backend)}")
        self.log(f"设置文件: {CONFIG_PATH}")

        # 定时器：200ms 刷新倒计时与日志队列；1s 驱动扫描调度
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(200)
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._on_scan_tick)
        self._scan_timer.start(1000)

        # 系统托盘：左键切换主窗口显示/隐藏；右键弹出精简状态菜单
        self._setup_tray()

        # 周期性重应用云母：Qt/系统事件可能静默清掉 DWM 属性（win32mica 也是反复重设的）
        if self._mica_supported:
            self._mica_timer = QTimer(self)
            self._mica_timer.timeout.connect(self._reapply_mica)
            self._mica_timer.start(3000)

    def _reapply_mica(self):
        if self._mica_applied and self._mica_supported:
            try:
                _apply_mica(int(self.winId()))
            except Exception:
                pass

    def showEvent(self, event):
        """窗口每次显示都重新应用云母（切换置顶等会重建句柄，属性会被清掉）。"""
        super().showEvent(event)
        if not self._mica_supported:
            return
        if _apply_mica(int(self.winId())):
            self._mica_applied = True
            if not self._mica_logged:
                self._mica_logged = True
                self.log("已启用 Windows 11 云母背景效果")
        elif not self._mica_applied:
            # 首次即失败（如系统实际不支持）：回退纯深色
            self._mica_supported = False
            self.setAttribute(Qt.WA_TranslucentBackground, False)
            QApplication.instance().setStyleSheet(QSS)
            self.log("当前系统不支持云母，使用纯深色背景")

    def _setup_tray(self):
        icon = make_tray_icon()
        self.setWindowIcon(icon)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip(APP_TITLE)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        self._tray_hint_shown = False
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.log("! 系统托盘不可用，关闭窗口时将直接退出")

    # ============================ UI 构建 ================================
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)

        self._build_timer_area(root)
        self._build_settings_area(root)

        # 样式表：云母模式下主背景透明；旧系统用纯深色
        if self._mica_supported:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            QApplication.instance().setStyleSheet(QSS_MICA)
        else:
            QApplication.instance().setStyleSheet(QSS)
        self._update_timer_display()
        self._set_compact_mode(True)  # 打开时默认收起模式

    def _build_timer_area(self, root):
        """顶部倒计时区 + 控制按钮行（收起模式也保留的部分）。"""
        # ---- 顶部：倒计时 ----
        self.lbl_phase = QLabel(PHASE_NAMES["work"])
        self.lbl_phase.setAlignment(Qt.AlignCenter)
        self.lbl_phase.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {DARK_PHASE_COLORS['work']};")
        root.addWidget(self.lbl_phase)

        self.lbl_time = QLabel("25:00")
        self.lbl_time.setAlignment(Qt.AlignCenter)
        self.lbl_time.setFont(QFont("Consolas", 48, QFont.Bold))
        self.lbl_time.setStyleSheet("color: #f5f6fa;")
        root.addWidget(self.lbl_time)

        self.lbl_status = QLabel("本轮番茄: 0/4   状态: 待开始   监管关闭")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setStyleSheet("color: #9d9d9d;")
        root.addWidget(self.lbl_status)

        # ---- 控制按钮 ----
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("开始")
        self.btn_pause = QPushButton("暂停")
        self.btn_skip = QPushButton("跳过")
        self.btn_reset = QPushButton("重置")
        self.btn_expand = QPushButton("展开设置 ▾")
        for b in (self.btn_start, self.btn_pause, self.btn_skip, self.btn_reset,
                  self.btn_expand):
            b.setMinimumHeight(30)
            btn_row.addWidget(b)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_skip.clicked.connect(self._on_skip)
        self.btn_reset.clicked.connect(self._on_reset)
        self.btn_expand.clicked.connect(self._toggle_compact)
        root.addLayout(btn_row)

    def _build_settings_area(self, root):
        """可收起设置区：两页导航（Tab1 设置·监管 + Tab2 统计）。"""
        self.settings_panel = QWidget()
        sp_layout = QVBoxLayout(self.settings_panel)
        sp_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        sp_layout.addWidget(self.tabs)

        # ======== Tab 1：设置·监管 ========
        self.tab_settings = QWidget()
        tv = QVBoxLayout(self.tab_settings)
        tv.setContentsMargins(8, 8, 8, 8)

        # ---- 番茄钟设置 ----
        grp_settings = QGroupBox("番茄钟设置")
        v = QVBoxLayout(grp_settings)

        # 第一行：工作时长 + 短休息
        row1 = QHBoxLayout()
        self.spin_work = self._spin(row1, "工作时长(分)", 0.1, 240, 1, 25.0)
        self.spin_short = self._spin(row1, "短休息(分)", 0.1, 120, 1, 5.0)
        row1.addStretch(1)
        v.addLayout(row1)

        # 第二行：长休息 + 长休息间隔
        row2 = QHBoxLayout()
        self.spin_long = self._spin(row2, "长休息(分)", 0.1, 120, 1, 15.0)
        self.spin_cycles = QSpinBox()
        self.spin_cycles.setRange(1, 20)
        self.spin_cycles.setValue(4)
        self.spin_cycles.setSuffix(" 个")
        row2.addWidget(self._lbl("长休息间隔"), 0)
        row2.addWidget(self.spin_cycles, 1)
        row2.addStretch(1)
        v.addLayout(row2)

        # 勾选选项：每个单独一行
        self.chk_autostart = QCheckBox("阶段结束后自动开始下一阶段")
        self.chk_autostart.setChecked(True)  # 与配置默认一致，避免启动时误触发保存
        self.chk_force_break = QCheckBox("进入休息时强制关闭监管应用")
        self.chk_topmost = QCheckBox("窗口总在最前")
        self.chk_sound = QCheckBox("状态切换提示音")
        self.chk_rest_overlay = QCheckBox("休息时全屏遮罩提醒(看向窗外/眨眼)")
        self.chk_presence = QCheckBox("摄像头在场检测(离开自动暂停/休息无人关遮罩)")
        self.chk_sound.setChecked(True)
        self.chk_rest_overlay.setChecked(True)
        self.chk_presence.setChecked(True)
        self.chk_autostart.toggled.connect(self._save_config)
        self.chk_force_break.toggled.connect(self._save_config)
        self.chk_topmost.toggled.connect(self._on_topmost_toggled)
        self.chk_sound.toggled.connect(self._save_config)
        self.chk_rest_overlay.toggled.connect(self._on_rest_overlay_toggled)
        self.chk_presence.toggled.connect(self._on_presence_toggled)
        v.addWidget(self.chk_autostart)
        v.addWidget(self.chk_force_break)
        v.addWidget(self.chk_topmost)
        v.addWidget(self.chk_sound)
        v.addWidget(self.chk_rest_overlay)
        v.addWidget(self.chk_presence)

        row4 = QHBoxLayout()
        row4.addWidget(self._lbl("专注离开宽限(秒)"))
        self.spin_presence_grace = QDoubleSpinBox()
        self.spin_presence_grace.setRange(5.0, 120.0)
        self.spin_presence_grace.setSingleStep(5.0)
        self.spin_presence_grace.setDecimals(0)
        self.spin_presence_grace.setValue(15.0)
        self.spin_presence_grace.valueChanged.connect(self._save_config)
        row4.addWidget(self.spin_presence_grace)
        row4.addStretch(1)
        v.addLayout(row4)

        row3 = QHBoxLayout()
        btn_apply = QPushButton("应用设置")
        btn_admin = QPushButton("以管理员身份重启")
        btn_apply.clicked.connect(self._apply_settings)
        btn_admin.clicked.connect(self._relaunch_admin)
        row3.addStretch(1)
        row3.addWidget(btn_apply)
        row3.addWidget(btn_admin)
        v.addLayout(row3)
        tv.addWidget(grp_settings)

        # ---- 应用监管 ----
        grp_mon = QGroupBox("应用监管")
        v = QVBoxLayout(grp_mon)

        # 勾选选项：每个单独一行
        mrow1 = QHBoxLayout()
        self.chk_monitor = QCheckBox("启用监管")
        self.chk_monitor.setChecked(True)
        self.chk_monitor.toggled.connect(self._toggle_monitor)
        mrow1.addWidget(self.chk_monitor)
        mrow1.addStretch(1)
        v.addLayout(mrow1)

        mrow2 = QHBoxLayout()
        self.chk_phase_gate = QCheckBox("仅工作阶段监管(休息/暂停/未开始不关闭)")
        self.chk_phase_gate.setChecked(True)
        self.chk_phase_gate.toggled.connect(self._save_config)
        mrow2.addWidget(self.chk_phase_gate)
        mrow2.addStretch(1)
        v.addLayout(mrow2)

        # 检测间隔单独一行
        mrow3 = QHBoxLayout()
        mrow3.addWidget(self._lbl("检测间隔(秒)"))
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.5, 60.0)
        self.spin_interval.setSingleStep(0.5)
        self.spin_interval.setDecimals(1)
        self.spin_interval.setValue(2.0)
        self.spin_interval.valueChanged.connect(self._save_config)
        mrow3.addWidget(self.spin_interval)
        mrow3.addStretch(1)
        v.addLayout(mrow3)

        mrow4 = QHBoxLayout()
        self.edit_app = QLineEdit()
        self.edit_app.setPlaceholderText("程序名，如 chrome 或 notepad.exe，回车添加")
        self.edit_app.returnPressed.connect(self._add_app)
        mrow4.addWidget(self.edit_app, 1)
        self.btn_add = QPushButton("添加")
        self.btn_del = QPushButton("删除选中")
        self.btn_clear = QPushButton("清空")
        self.btn_add.clicked.connect(self._add_app)
        self.btn_del.clicked.connect(self._remove_app)
        self.btn_clear.clicked.connect(self._clear_apps)
        mrow4.addWidget(self.btn_add)
        mrow4.addWidget(self.btn_del)
        mrow4.addWidget(self.btn_clear)
        v.addLayout(mrow4)

        self.list_apps = QListWidget()
        self.list_apps.setMaximumHeight(110)
        v.addWidget(self.list_apps)
        tv.addWidget(grp_mon)

        # ---- 日志 ----
        grp_log = QGroupBox("日志")
        v = QVBoxLayout(grp_log)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(2000)
        v.addWidget(self.log_text)
        tv.addWidget(grp_log, 1)
        self.tabs.addTab(self.tab_settings, "设置·监管")

        # ======== Tab 2：统计 ========
        self.stats_panel = StatsPanel(self._stats)
        self.tabs.addTab(self.stats_panel, "统计")
        self.tabs.setCurrentIndex(0)

        root.addWidget(self.settings_panel, 1)

    def _lbl(self, text):
        return QLabel(text)

    def _spin(self, layout, label, lo, hi, step, default):
        """一行里放一个带标签的 QDoubleSpinBox，并返回控件。"""
        layout.addWidget(self._lbl(label))
        sb = QDoubleSpinBox()
        sb.setRange(lo, hi)
        sb.setSingleStep(step)
        sb.setDecimals(1)
        sb.setValue(default)
        sb.valueChanged.connect(self._on_duration_changed)
        layout.addWidget(sb)
        return sb

    def _on_duration_changed(self, _v):
        """时长变化即保存，用户点「应用设置」才真正生效。"""
        self._save_config()

    # ============================ 番茄钟逻辑 ==============================
    def _apply_settings(self):
        try:
            self.engine.set_durations(self.spin_work.value(), self.spin_short.value(),
                                      self.spin_long.value(), self.spin_cycles.value())
        except (ValueError, TypeError):
            QMessageBox.warning(self, "设置无效", "请检查时长设置是否为有效数字。")
            return
        self.log(f"已更新番茄钟设置: 工作{self.spin_work.value()}分 / "
                 f"短休{self.spin_short.value()}分 / 长休{self.spin_long.value()}分 / "
                 f"每{self.spin_cycles.value()}个番茄长休")
        self._save_config()
        self._update_timer_display()

    def _on_start(self):
        self.engine.start()
        self._presence_paused = False
        self.log(f"开始 {PHASE_NAMES[self.engine.phase]}")
        self._update_timer_display()

    def _on_pause(self):
        eng = self.engine
        if eng.state == "running":
            eng.pause()
            self._presence_paused = False  # 手动暂停：不会被摄像头自动恢复
            self.log("已暂停")
        elif eng.state == "paused":
            eng.resume()
            self._presence_paused = False  # 手动恢复：接管控制权（人不在会再次自动暂停）
            self.log("继续计时")
        self._update_timer_display()

    def _on_skip(self):
        if self.engine.state == "idle":
            self._on_start()
            return
        if self.engine.phase == "work":
            self._log_work_segment(done=False)  # 跳过：已运行时间照常计入
        self.engine.skip()
        self._presence_paused = False
        self.log(f"⏰ 进入 {PHASE_NAMES[self.engine.phase]}")
        play_state_sound(self.engine.phase, self.chk_sound.isChecked())
        self._sync_rest_overlay(self.engine.phase)
        self._update_timer_display()

    def _on_reset(self):
        self.engine.reset()
        self._presence_paused = False
        self.log("已重置计时器")
        self._hide_rest_overlay()
        self._update_timer_display()

    def _on_tick(self):
        eng = self.engine
        if eng.state == "running" and eng.remaining_seconds() <= 0:
            if eng.phase == "work":
                self._log_work_segment(done=True)  # 自然结束的番茄
            new_phase, finished_work = eng.advance()
            self.log(f"⏰ {PHASE_NAMES[new_phase]} 开始"
                     + (f"（已完成{eng.work_count}个番茄）" if finished_work else ""))
            play_state_sound(new_phase, self.chk_sound.isChecked())
            if self.isVisible():
                self._flash_window()  # 窗口隐藏到托盘时不弹窗
            # 休息开始时按需强制关闭监管应用
            if (new_phase != "work" and self.chk_force_break.isChecked()
                    and self.monitored):
                self.log("—— 进入休息，开始强制关闭监管应用 ——")
                self._trigger_scan(force=True)
            # 休息/工作阶段切换时同步全屏遮罩
            self._sync_rest_overlay(new_phase)
            self._presence_paused = False  # 新阶段重新开始"离开"判定
            if not self.chk_autostart.isChecked():
                eng.pause()
        # 摄像头在场检测：按状态调度启停 + 应用离开暂停/恢复/关遮罩规则
        self._update_presence()
        self._check_presence_rules()
        self._update_timer_display()
        self._update_tray_tooltip()
        self._flush_log_queue()

    # ============================ 窗口显示模式 =============================
    def _set_compact_mode(self, compact, move_corner=False):
        """切换主窗口显示模式：True=收起（仅番茄钟+功能按钮），False=展开（含设置配置）。

        move_corner=True 时把窗口移到屏幕右上角（休息时自动收起用）。
        程序只会自动收起，从不自动展开——展开只能由用户点按钮。
        """
        if compact == self._compact:
            if compact and move_corner:
                self._move_top_right()
            return
        if compact:
            # 收起：记录展开尺寸 → 隐藏设置区 → 收窄窗口高度
            self._expanded_size = self.size()
            self._compact = True
            self.settings_panel.hide()
            self.setMinimumSize(COMPACT_W, COMPACT_H)
            self.resize(self.width(), COMPACT_H)
            self.btn_expand.setText("展开面板 ▾")
            if move_corner:
                self._move_top_right()
        else:
            # 展开：恢复之前的展开尺寸（用户手动操作），默认落在「统计」页
            self._compact = False
            self.settings_panel.show()
            self.tabs.setCurrentIndex(1)  # 统计页
            self.setMinimumSize(EXPANDED_MIN_W, EXPANDED_MIN_H)
            target = self._expanded_size or QSize(EXPANDED_MIN_W, EXPANDED_MIN_H)
            self.resize(target)
            self.btn_expand.setText("收起面板 ▾")
            # 右上角小窗展开后会超高，确保窗口整体在屏幕内
            avail = self.screen().availableGeometry()
            if self.y() + self.height() > avail.bottom():
                self.move(self.x(), max(avail.top(), avail.bottom() - self.height()))

    def _toggle_compact(self):
        """用户手动展开/收起窗口（不移动位置）。"""
        self._set_compact_mode(not self._compact)
        self.log("已切换为" + ("收起" if self._compact else "展开") + "模式")

    def _enter_rest_compact(self):
        """进入休息时自动收起并移到右上角（程序不参与自动展开）。"""
        self._set_compact_mode(True, move_corner=True)
        self.log("休息模式：窗口已收起至右上角")

    def _move_top_right(self):
        """移到所在屏幕右上角（距右/上缘 TOP_RIGHT_MARGIN 像素）。"""
        avail = self.screen().availableGeometry()
        x = max(avail.x(), avail.right() - self.width() - TOP_RIGHT_MARGIN)
        self.move(x, avail.top() + TOP_RIGHT_MARGIN)

    # ============================ 休息全屏遮罩 =============================
    def _sync_rest_overlay(self, phase):
        """按当前阶段同步休息遮罩与窗口模式：休息显示遮罩并自动收起，工作隐藏。"""
        if phase == "work":
            self._hide_rest_overlay()
        else:
            self._show_rest_overlay()
            self._enter_rest_compact()  # 休息时自动收起窗口并移右上角

    def _show_rest_overlay(self):
        if not self.chk_rest_overlay.isChecked():
            return
        if self._overlay is None:
            self._overlay = RestOverlay()
        self._overlay.show_overlay()
        # 只有本软件窗口在遮罩上方：主窗口保持可见并压住遮罩
        if not self.isVisible():
            self.showNormal()
        if not self.chk_topmost.isChecked():
            self._topmost_forced = True
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()
        self.raise_()
        self.activateWindow()
        self._avoid_overlay_text()
        self.log("—— 休息遮罩已显示，请看向6米外物体并眨眼 ——")

    def _hide_rest_overlay(self):
        if self._overlay is not None and self._overlay.isVisible():
            self._overlay.hide()
            self.log("休息遮罩已关闭")
        if self._topmost_forced:
            self._topmost_forced = False
            # 恢复与用户勾选一致的置顶状态
            self.setWindowFlag(Qt.WindowStaysOnTopHint,
                               self.chk_topmost.isChecked())
            self.show()

    def _avoid_overlay_text(self):
        """主窗口若覆盖屏幕中央（遮罩文本区），移到所在屏幕底部居中，避免遮挡文字。"""
        if self._overlay is None:
            return
        geo = self.geometry()
        vg = RestOverlay._virtual_geometry()
        center = QRect(vg.x() + int(vg.width() * 0.25),
                       vg.y() + int(vg.height() * 0.25),
                       int(vg.width() * 0.5), int(vg.height() * 0.5))
        if not geo.intersects(center):
            return
        avail = self.screen().availableGeometry()
        x = max(avail.x(), avail.center().x() - geo.width() // 2)
        x = min(x, avail.right() - geo.width() + 1)
        self.move(x, avail.bottom() - geo.height() - 20)

    def _on_rest_overlay_toggled(self, checked):
        """勾选/取消「休息全屏遮罩」：即时保存，并立刻同步当前阶段。"""
        self._save_config()
        if checked:
            if self.engine.state != "idle" and self.engine.phase != "work":
                self._show_rest_overlay()
        else:
            self._hide_rest_overlay()

    # ============================ 摄像头在场检测 =============================
    def _presence_should_run(self):
        """当前状态是否需要启用摄像头检测。"""
        if not self.chk_presence.isChecked():
            return False
        eng = self.engine
        if self._overlay is not None and self._overlay.isVisible():
            return True  # 休息遮罩显示中：人在保持遮罩，离开超时关闭
        if eng.phase == "work" and (eng.state == "running" or self._presence_paused):
            return True  # 工作计时中，或"离开暂停"等待自动恢复
        return False

    def _update_presence(self):
        """按当前状态启停摄像头检测（幂等，每 tick 调用）。"""
        if not self.chk_presence.isChecked():
            if self._presence is not None:
                self._presence.disable()
            return
        if not self._presence_should_run():
            if self._presence is not None:
                self._presence.disable()
            return
        if self._presence is None:
            self._presence = PresenceDetector()
            self._presence_err_logged = False
        self._presence.enable()

    def _check_presence_rules(self):
        """应用摄像头规则：专注离开暂停/回来自动恢复/休息无人关遮罩。"""
        det = self._presence
        if det is None:
            return
        if not det.is_available():
            # 降级提示（只记录一次，避免刷屏）
            err = det.error()
            if err and not self._presence_err_logged:
                self._presence_err_logged = True
                self.log(f"! 摄像头检测不可用，已降级: {err}")
            return
        self._presence_err_logged = False
        eng = self.engine
        if eng.phase == "work" and eng.state == "running":
            # 专注：人不在超过宽限期 → 自动暂停
            if (not det.is_present()
                    and det.last_seen_age() > self.spin_presence_grace.value()):
                self._presence_paused = True
                eng.pause()
                self.log("摄像头未检测到人，自动暂停计时")
                self._update_timer_display()
        elif (eng.phase == "work" and eng.state == "paused"
              and self._presence_paused):
            # 离开暂停：人回到摄像头前 → 自动恢复
            if det.is_present():
                self._presence_paused = False
                eng.resume()
                self.log("检测到人回到摄像头前，自动继续计时")
                self._update_timer_display()
        elif (eng.phase != "work" and self._overlay is not None
              and self._overlay.isVisible()):
            # 休息：人不在超过 1 分钟 → 关闭遮罩（本次休息不再重新打开）
            if not det.is_present() and det.last_seen_age() > REST_AWAY_SECONDS:
                self.log("超过1分钟未检测到人，关闭休息遮罩")
                self._hide_rest_overlay()
                self._update_timer_display()

    def _presence_status(self):
        """摄像头检测状态文本（状态栏显示）。"""
        if not self.chk_presence.isChecked():
            return "摄像头: 关"
        det = self._presence
        if det is None or not det.is_available():
            return "摄像头: 不可用"
        if not self._presence_should_run():
            return "摄像头: 待命"
        return "摄像头: 人在" if det.is_present() else "摄像头: 人不在"

    def _on_presence_toggled(self, checked):
        """勾选/取消「摄像头在场检测」：即时保存并刷新检测器。"""
        self._save_config()
        if not checked:
            self._presence_paused = False
            if self._presence is not None:
                self._presence.disable()
        self._update_timer_display()

    def _update_tray_tooltip(self):
        """托盘悬浮提示：阶段 + 剩余时间。"""
        if self._tray is not None:
            eng = self.engine
            self._tray.setToolTip(
                f"{APP_TITLE}\n{PHASE_NAMES[eng.phase]} · "
                f"{self._fmt_remaining()} 剩余")

    def _flash_window(self):
        """阶段切换时闪烁提示（临时置顶）。"""
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.show()
        QTimer.singleShot(600, self._restore_topmost)

    def _restore_topmost(self):
        # 遮罩强制置顶期间，保持主窗口在最上（否则会被遮罩压住）
        want = self._topmost_forced or self.chk_topmost.isChecked()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, want)
        self.show()

    def _fmt_remaining(self, seconds=None):
        """剩余秒数 → "MM:SS"（向上取整；供状态栏/托盘/菜单共用）。"""
        if seconds is None:
            seconds = self.engine.remaining_seconds()
        remaining = int(seconds + 0.999)
        mm, ss = divmod(remaining, 60)
        return f"{mm:02d}:{ss:02d}"

    def _monitor_status_text(self):
        """当前监管状态的界面文案（状态栏/托盘菜单共用）。"""
        if not self.chk_monitor.isChecked():
            return "监管关闭"
        if self._enforcement_active():
            return "监管中"
        if self.chk_phase_gate.isChecked():
            return "监管暂停(休息/未开始)"
        return "监管中"

    def _update_timer_display(self):
        eng = self.engine
        self.lbl_time.setText(self._fmt_remaining())
        phase = eng.phase
        self.lbl_phase.setText(PHASE_NAMES[phase])
        self.lbl_phase.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {DARK_PHASE_COLORS[phase]};")

        state = STATE_NAMES[eng.state]
        self.lbl_status.setText(
            f"本轮番茄: {eng.work_count}/{eng.cycles_before_long}   状态: {state}   "
            f"{self._monitor_status_text()}   {self._presence_status()}")

        # 按钮状态
        self.btn_start.setEnabled(eng.state == "idle")
        self.btn_pause.setEnabled(eng.state != "idle")
        self.btn_pause.setText("继续" if eng.state == "paused" else "暂停")
        # 统计面板实时刷新（轻量：文本/图表数据更新）
        self.stats_panel.refresh(self._live_focus_extra())

    def _log_work_segment(self, done):
        """把当前工作阶段已运行时长写入统计（调用前须处于 work 阶段且未 advance）。"""
        eng = self.engine
        if eng.phase != "work":
            return
        elapsed = eng.work_sec - eng.remaining_seconds()
        self._stats.add_work_segment(elapsed, done=done)

    def _live_focus_extra(self):
        """当前工作阶段已运行（未落库）的秒数，用于统计页今日档实时叠加。"""
        eng = self.engine
        if eng.phase == "work" and eng.state in ("running", "paused"):
            return int(eng.work_sec - eng.remaining_seconds())
        return 0

    # ============================ 应用监管 ================================
    def _toggle_monitor(self):
        self.log("监管已启用" if self.chk_monitor.isChecked() else "监管已关闭")
        self._save_config()
        self._update_timer_display()

    def _on_topmost_toggled(self, checked):
        """「窗口总在最前」勾选即时生效并保存。"""
        if self._topmost_forced:
            self._save_config()
            return  # 遮罩期间由遮罩逻辑统一管理，退出遮罩时恢复勾选状态
        self.setWindowFlag(Qt.WindowStaysOnTopHint, checked)
        self.show()
        self._save_config()

    def _enforcement_active(self):
        """当前是否应当执行监管扫描。"""
        if not self.chk_monitor.isChecked():
            return False
        if not self.chk_phase_gate.isChecked():
            return True  # 用户选择始终监管
        eng = self.engine
        return eng.state == "running" and eng.phase == "work"

    def _on_scan_tick(self):
        interval = max(0.5, self.spin_interval.value())
        now = time.monotonic()
        if now - self._last_scan >= interval:
            self._last_scan = now
            if self.monitored and self._enforcement_active():
                self._trigger_scan()

    def _trigger_scan(self, force=False):
        """在后台线程执行一次扫描+关闭，避免阻塞界面。"""
        if self._scan_busy:
            return  # 上一轮还没结束，跳过本轮，不堆积任务
        self._scan_busy = True
        threading.Thread(target=self._scan_worker, args=(force,),
                         daemon=True).start()

    def _scan_worker(self, force):
        """后台线程：批量检测 -> 逐个关闭 -> 日志经队列回写主线程。"""
        try:
            names = list(self.monitored)
            if not names:
                return
            try:
                running = self.pm.find_running(names)
            except Exception as exc:
                self._ui_log(f"! 检测失败: {exc}")
                return
            now = time.monotonic()
            for exe in running:
                # 冷却期内不再反复尝试同一失败，避免日志刷屏
                if not force and now < self._fail_cooldown.get(exe, 0):
                    continue
                ok, detail = self.pm.kill(exe)
                if ok:
                    self._fail_cooldown.pop(exe, None)
                    self._fail_msg.pop(exe, None)
                    self._ui_log(f"✂ {time.strftime('%H:%M:%S')} 已强制关闭 {exe}（{detail}）")
                    if force:
                        break
                else:
                    if detail == self._fail_msg.get(exe):
                        self._fail_cooldown[exe] = time.monotonic() + 60
                        self._ui_log(f"! 关闭 {exe} 失败: {detail}；同一错误 60 秒内不再重试")
                    else:
                        self._fail_msg[exe] = detail
                        msg = f"! 关闭 {exe} 失败: {detail}"
                        low = detail.lower()
                        if "权限" in detail or "denied" in low or "拒绝" in detail:
                            msg += "；目标可能以管理员权限运行或受反作弊保护，建议以管理员身份运行本程序"
                        self._ui_log(msg)
        finally:
            self._scan_busy = False

    def _add_app(self):
        name = self.edit_app.text().strip()
        if not name:
            return
        exe = normalize_exe(name)
        if exe in self.monitored:
            self.log(f"{exe} 已在监管列表中")
            self.edit_app.clear()
            return
        self.monitored.append(exe)
        self.list_apps.addItem(exe)
        self.edit_app.clear()
        self.log(f"已加入监管: {exe}")
        self._save_config()

    def _remove_app(self):
        for item in reversed(self.list_apps.selectedItems()):
            exe = item.text()
            row = self.list_apps.row(item)
            self.list_apps.takeItem(row)
            if exe in self.monitored:
                self.monitored.remove(exe)
            self._fail_cooldown.pop(exe, None)
            self._fail_msg.pop(exe, None)
        self.log("已删除选中的监管项")
        self._save_config()

    def _clear_apps(self):
        self.monitored.clear()
        self.list_apps.clear()
        self._fail_cooldown.clear()
        self._fail_msg.clear()
        self.log("已清空监管列表")
        self._save_config()

    # ============================ 设置持久化 ==============================
    def _load_config_to_ui(self):
        cfg = self.cfg
        timer = cfg.get("timer", {})
        self.spin_work.setValue(timer.get("work_min", 25))
        self.spin_short.setValue(timer.get("short_min", 5))
        self.spin_long.setValue(timer.get("long_min", 15))
        self.spin_cycles.setValue(timer.get("cycles", 4))
        try:
            self.engine.set_durations(self.spin_work.value(), self.spin_short.value(),
                                      self.spin_long.value(), self.spin_cycles.value())
        except (ValueError, TypeError):
            pass
        self.spin_interval.setValue(cfg.get("interval", 2.0))
        self.chk_monitor.setChecked(cfg.get("monitor_enabled", True))
        self.chk_phase_gate.setChecked(cfg.get("phase_gate", True))
        self.chk_force_break.setChecked(cfg.get("force_on_break", False))
        self.chk_autostart.setChecked(cfg.get("autostart", True))
        self.chk_topmost.setChecked(cfg.get("topmost", False))
        self.chk_sound.setChecked(cfg.get("sound_enabled", True))
        self.chk_rest_overlay.setChecked(cfg.get("rest_overlay", True))
        self.chk_presence.setChecked(cfg.get("presence_enabled", True))
        self.spin_presence_grace.setValue(cfg.get("presence_grace_sec", 15.0))
        if self.chk_topmost.isChecked():
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()
        for exe in cfg.get("monitored", []):
            exe = normalize_exe(exe)
            if exe not in self.monitored:
                self.monitored.append(exe)
                self.list_apps.addItem(exe)
        if self.monitored:
            self.log(f"已从设置文件加载 {len(self.monitored)} 个监管目标")
        self._update_timer_display()

    def _save_config(self):
        cfg = {
            "timer": {
                "work_min": self.spin_work.value(),
                "short_min": self.spin_short.value(),
                "long_min": self.spin_long.value(),
                "cycles": self.spin_cycles.value(),
            },
            "monitored": list(self.monitored),
            "interval": self.spin_interval.value(),
            "monitor_enabled": self.chk_monitor.isChecked(),
            "phase_gate": self.chk_phase_gate.isChecked(),
            "force_on_break": self.chk_force_break.isChecked(),
            "autostart": self.chk_autostart.isChecked(),
            "topmost": self.chk_topmost.isChecked(),
            "sound_enabled": self.chk_sound.isChecked(),
            "rest_overlay": self.chk_rest_overlay.isChecked(),
            "presence_enabled": self.chk_presence.isChecked(),
            "presence_grace_sec": self.spin_presence_grace.value(),
        }
        if not save_config(cfg):
            self.log(f"! 设置保存失败: {CONFIG_PATH}")

    # ============================ 系统托盘 ================================
    def _on_tray_activated(self, reason):
        """托盘点击：左键切换主窗口；右键弹出精简状态菜单。

        注意只处理 Trigger(单击左键)：部分平台双击会同时发 Trigger 和
        DoubleClick，若两者都处理会连续开关两次等于没反应。
        """
        if reason == QSystemTrayIcon.Trigger:
            self._toggle_window()
        elif reason == QSystemTrayIcon.Context:
            self._show_tray_menu()

    def _toggle_window(self):
        if self.isVisible():
            self.hide()  # 从任务栏消失，后台运行
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _show_tray_menu(self):
        """在光标处弹出托盘菜单（每次现构建，保证状态最新）。"""
        self._build_tray_menu().exec(QCursor.pos())

    def _build_tray_menu(self):
        """构建精简状态菜单（可独立测试，不含 exec）。"""
        eng = self.engine
        state = STATE_NAMES[eng.state]

        menu = QMenu(self)
        for text in (f"阶段: {PHASE_NAMES[eng.phase]} · {state}",
                     f"剩余: {self._fmt_remaining()}",
                     f"本轮番茄: {eng.work_count}/{eng.cycles_before_long}",
                     f"监管: {self._monitor_status_text()}"):
            act = QAction(text, menu)
            act.setEnabled(False)  # 只读状态行
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction("显示/隐藏主窗口", self._toggle_window)
        if eng.state == "idle":
            menu.addAction("开始", self._on_start)
        else:
            menu.addAction("暂停/继续", self._on_pause)
        menu.addAction("跳过", self._on_skip)
        menu.addAction("重置", self._on_reset)
        menu.addSeparator()
        menu.addAction("退出", self._quit_app)
        return menu

    def _quit_app(self):
        """真正退出程序（托盘菜单专用）。"""
        self._hide_rest_overlay()
        if self._presence is not None:
            self._presence.stop()  # 释放摄像头并停止检测线程
        self._log_work_segment(done=False)  # 退出前把当前工作段落库（不丢）
        self._stats.close()
        if self._tray is not None:
            self._tray.hide()
        QApplication.instance().quit()

    # ============================ 其他 ====================================
    def _relaunch_admin(self):
        """以管理员身份重启本程序（用于关闭需要更高权限的目标进程）。"""
        if QMessageBox.question(
                self, "以管理员身份重启",
                "将关闭当前窗口并触发 UAC 提权重启。\n"
                "提权后需重新添加监管目标。是否继续？") != QMessageBox.Yes:
            return
        if getattr(sys, "frozen", False):
            # 打包后：sys.executable 就是 exe 本身，直接提权重启 exe
            target, arglist = sys.executable, ""
        else:
            script = os.path.abspath(sys.argv[0] or __file__)
            if not os.path.exists(script):
                script = os.path.abspath(__file__)
            target, arglist = sys.executable, f'"{script}"'
        ps_cmd = ("$py = '{0}'; $args = '{1}'; "
                  "Start-Process -FilePath $py -ArgumentList $args -Verb RunAs"
                  ).format(target.replace("'", "''"), arglist.replace("'", "''"))
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                creationflags=0x08000000 if os.name == "nt" else 0)
        except Exception as exc:
            QMessageBox.warning(self, "启动失败", f"无法触发管理员启动: {exc}")
            return
        self._quit_app()

    def log(self, text):
        ts = time.strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{ts}] {text}")

    def _ui_log(self, text):
        """从后台线程安全地向主线程日志区追加一行（经队列中转）。"""
        self._log_queue.put(text)

    def _flush_log_queue(self):
        """主线程在定时器里冲刷日志队列（线程安全的 UI 更新方式）。"""
        while True:
            try:
                text = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.log(text)

    def closeEvent(self, event):
        """关闭按钮 = 隐藏到托盘后台运行；真正退出走托盘菜单。"""
        if self._tray is not None and QSystemTrayIcon.isSystemTrayAvailable():
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.log("已最小化到托盘，点击托盘图标恢复；右键托盘图标可退出")
            self.hide()
            event.ignore()
        elif QMessageBox.question(self, "退出", "确定要退出番茄钟·应用监管吗？") == QMessageBox.Yes:
            event.accept()
        else:
            event.ignore()

    def changeEvent(self, event):
        """最小化时自动隐藏到托盘（从任务栏消失）。"""
        if event.type() == QEvent.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    lock = acquire_lock()
    if lock is None:
        QMessageBox.information(
            None, "已在运行",
            "番茄钟·应用监管已在后台运行。\n请点击系统托盘中的番茄图标打开主窗口。")
        return 0

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
