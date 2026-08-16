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

from PySide6.QtCore import QEvent, QLockFile, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from pomodoro_core import (
    CONFIG_PATH, PHASE_NAMES, ProcessManager, PomodoroEngine,
    load_config, normalize_exe, save_config,
)

APP_TITLE = "番茄钟 · 应用监管"

# 单例锁：位于系统临时目录（避免脚本目录只读导致锁创建失败）
LOCK_PATH = os.path.join(tempfile.gettempdir(), "pomodoro_guard_qt.lock")


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        # 最小宽度 = 启动宽度；高度适当增大以容纳逐行布局
        self.resize(500, 860)
        self.setMinimumWidth(500)
        self.setMinimumHeight(720)

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
        for b in (self.btn_start, self.btn_pause, self.btn_skip, self.btn_reset):
            b.setMinimumHeight(30)
            btn_row.addWidget(b)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_skip.clicked.connect(self._on_skip)
        self.btn_reset.clicked.connect(self._on_reset)
        root.addLayout(btn_row)

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
        self.chk_force_break = QCheckBox("进入休息时强制关闭监管应用")
        self.chk_topmost = QCheckBox("窗口总在最前")
        self.chk_sound = QCheckBox("状态切换提示音")
        self.chk_sound.setChecked(True)
        self.chk_autostart.toggled.connect(self._save_config)
        self.chk_force_break.toggled.connect(self._save_config)
        self.chk_topmost.toggled.connect(self._on_topmost_toggled)
        self.chk_sound.toggled.connect(self._save_config)
        v.addWidget(self.chk_autostart)
        v.addWidget(self.chk_force_break)
        v.addWidget(self.chk_topmost)
        v.addWidget(self.chk_sound)

        row3 = QHBoxLayout()
        btn_apply = QPushButton("应用设置")
        btn_admin = QPushButton("以管理员身份重启")
        btn_apply.clicked.connect(self._apply_settings)
        btn_admin.clicked.connect(self._relaunch_admin)
        row3.addStretch(1)
        row3.addWidget(btn_apply)
        row3.addWidget(btn_admin)
        v.addLayout(row3)
        root.addWidget(grp_settings)

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
        root.addWidget(grp_mon)

        # ---- 日志 ----
        grp_log = QGroupBox("日志")
        v = QVBoxLayout(grp_log)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(2000)
        v.addWidget(self.log_text)
        root.addWidget(grp_log, 1)

        # 样式表：云母模式下主背景透明；旧系统用纯深色
        if self._mica_supported:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            QApplication.instance().setStyleSheet(QSS_MICA)
        else:
            QApplication.instance().setStyleSheet(QSS)
        self._update_timer_display()

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
        self.log(f"开始 {PHASE_NAMES[self.engine.phase]}")
        self._update_timer_display()

    def _on_pause(self):
        eng = self.engine
        if eng.state == "running":
            eng.pause()
            self.log("已暂停")
        elif eng.state == "paused":
            eng.resume()
            self.log("继续计时")
        self._update_timer_display()

    def _on_skip(self):
        if self.engine.state == "idle":
            self._on_start()
            return
        self.engine.skip()
        self.log(f"⏰ 进入 {PHASE_NAMES[self.engine.phase]}")
        play_state_sound(self.engine.phase, self.chk_sound.isChecked())
        self._update_timer_display()

    def _on_reset(self):
        self.engine.reset()
        self.log("已重置计时器")
        self._update_timer_display()

    def _on_tick(self):
        eng = self.engine
        if eng.state == "running" and eng.remaining_seconds() <= 0:
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
            if not self.chk_autostart.isChecked():
                eng.pause()
        self._update_timer_display()
        self._update_tray_tooltip()
        self._flush_log_queue()

    def _update_tray_tooltip(self):
        """托盘悬浮提示：阶段 + 剩余时间。"""
        if self._tray is not None:
            eng = self.engine
            remaining = int(eng.remaining_seconds() + 0.999)
            mm, ss = divmod(remaining, 60)
            self._tray.setToolTip(
                f"{APP_TITLE}\n{PHASE_NAMES[eng.phase]} · {mm:02d}:{ss:02d} 剩余")

    def _flash_window(self):
        """阶段切换时闪烁提示（临时置顶）。"""
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.show()
        QTimer.singleShot(600, self._restore_topmost)

    def _restore_topmost(self):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.chk_topmost.isChecked())
        self.show()

    def _update_timer_display(self):
        eng = self.engine
        remaining = int(eng.remaining_seconds() + 0.999)
        mm, ss = divmod(remaining, 60)
        self.lbl_time.setText(f"{mm:02d}:{ss:02d}")
        phase = eng.phase
        self.lbl_phase.setText(PHASE_NAMES[phase])
        self.lbl_phase.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {DARK_PHASE_COLORS[phase]};")

        if not self.chk_monitor.isChecked():
            mon = "监管关闭"
        elif self._enforcement_active():
            mon = "监管中"
        elif self.chk_phase_gate.isChecked():
            mon = "监管暂停(休息/未开始)"
        else:
            mon = "监管中"
        state = {"running": "进行中", "paused": "已暂停", "idle": "待开始"}[eng.state]
        self.lbl_status.setText(
            f"本轮番茄: {eng.work_count}/{eng.cycles_before_long}   状态: {state}   {mon}")

        # 按钮状态
        self.btn_start.setEnabled(eng.state == "idle")
        self.btn_pause.setEnabled(eng.state != "idle")
        self.btn_pause.setText("继续" if eng.state == "paused" else "暂停")

    # ============================ 应用监管 ================================
    def _toggle_monitor(self):
        self.log("监管已启用" if self.chk_monitor.isChecked() else "监管已关闭")
        self._save_config()
        self._update_timer_display()

    def _on_topmost_toggled(self, checked):
        """「窗口总在最前」勾选即时生效并保存。"""
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
        remaining = int(eng.remaining_seconds() + 0.999)
        mm, ss = divmod(remaining, 60)
        state = {"running": "进行中", "paused": "已暂停", "idle": "待开始"}[eng.state]
        if not self.chk_monitor.isChecked():
            mon = "监管关闭"
        elif self._enforcement_active():
            mon = "监管中"
        elif self.chk_phase_gate.isChecked():
            mon = "监管暂停(休息/未开始)"
        else:
            mon = "监管中"

        menu = QMenu(self)
        for text in (f"阶段: {PHASE_NAMES[eng.phase]} · {state}",
                     f"剩余: {mm:02d}:{ss:02d}",
                     f"本轮番茄: {eng.work_count}/{eng.cycles_before_long}",
                     f"监管: {mon}"):
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
