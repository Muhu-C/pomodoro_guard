# -*- coding: utf-8 -*-
"""主窗口 (MainWindow) — 纯 GUI 层。

只负责 UI 布局、控件创建和信号绑定；所有业务逻辑通过调用 controller 方法实现。
目标：< 400 行。
"""

import os
import subprocess
import sys
import time

from PySide6.QtCore import QEvent, QLockFile, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from core.utils import PHASE_NAMES, normalize_exe
from gui.overlay import RestOverlay
from gui.theme import (COMPACT_H, COMPACT_W, DARK_PHASE_COLORS, EXPANDED_MIN_H,
                       EXPANDED_MIN_W, QSS, QSS_MICA, TOP_RIGHT_MARGIN,
                       apply_mica, play_state_sound, supports_mica)
from gui.tray_icon import APP_TITLE, STATE_NAMES, TrayIcon, make_tray_icon
from gui.widgets import StatsPanel


# 单例锁路径
LOCK_PATH = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
                         "pomodoro_guard_qt.lock")


def acquire_lock():
    """尝试获取单例锁。返回持有锁的 QLockFile 对象，若已有实例在运行则返回 None。"""
    lock = QLockFile(LOCK_PATH)
    lock.removeStaleLockFile()
    if lock.tryLock(100):
        return lock
    return None


class MainWindow(QMainWindow):
    """主窗口：只负责 UI 布局和信号绑定，业务逻辑全部委托给 controller。"""

    def __init__(self, controller):
        super().__init__()
        self._ctrl = controller
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_tray_icon())  # 与旧版一致：窗口/托盘同款番茄图标
        self.resize(400, 900)
        self.setMinimumWidth(400)
        self.setMinimumHeight(890)

        # 状态标记
        self._mica_supported = supports_mica()
        self._mica_applied = False
        self._mica_logged = False
        self._overlay = None
        self._topmost_forced = False
        self._compact = False
        self._expanded_size = None

        self._build_ui()
        self._load_config_to_ui()

        # 连接 controller 信号
        self._ctrl.tick_updated.connect(self._update_display)
        self._ctrl.phase_changed.connect(self._on_phase_changed)
        self._ctrl.log_appended.connect(self._append_log)
        self._ctrl.overlay_requested.connect(self._handle_overlay_request)
        self._ctrl.presence_alert.connect(self._show_presence_alert)

        # 系统托盘
        self._tray = TrayIcon(self._ctrl, self, self)

        # 云母重应用定时器
        if self._mica_supported:
            self._mica_timer = QTimer(self)
            self._mica_timer.timeout.connect(self._reapply_mica)
            self._mica_timer.start(3000)

        # 启动日志
        self._append_log(f"程序启动，检测后端: {self._ctrl.process_backend_name()}")
        self._append_log(f"设置文件: {self._ctrl.config_path()}")
        minimizer_ok, minimizer_err = self._ctrl.minimizer_status()
        if not minimizer_ok:
            self._append_log(f"! 工作时段最小化功能不可用: {minimizer_err}，该功能将保持关闭")

    # =====================================================================
    #  UI 构建
    # =====================================================================

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)

        self._build_timer_area(root)
        self._build_settings_area(root)

        # 样式表
        if self._mica_supported:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            QApplication.instance().setStyleSheet(QSS_MICA)
        else:
            QApplication.instance().setStyleSheet(QSS)
        self._update_display()
        self._set_compact_mode(True)

    def _build_timer_area(self, root):
        """顶部倒计时区 + 控制按钮行。"""
        self.lbl_phase = QLabel(PHASE_NAMES["work"])
        self.lbl_phase.setAlignment(Qt.AlignCenter)
        self.lbl_phase.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {DARK_PHASE_COLORS['work']};")
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
        self.btn_start.clicked.connect(self._ctrl.start_timer)
        self.btn_pause.clicked.connect(self._on_pause_clicked)
        self.btn_skip.clicked.connect(self._on_skip_clicked)
        self.btn_reset.clicked.connect(self._ctrl.reset_timer)
        self.btn_expand.clicked.connect(self._toggle_compact)
        root.addLayout(btn_row)

    def _build_settings_area(self, root):
        """可收起设置区：两页导航。"""
        self.settings_panel = QWidget()
        sp_layout = QVBoxLayout(self.settings_panel)
        sp_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        sp_layout.addWidget(self.tabs)

        # Tab 1: 设置·监管
        self.tab_settings = QWidget()
        root_layout = QVBoxLayout(self.tab_settings)
        root_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(8, 8, 8, 8)
        self.scroll_layout.setSpacing(8)
        self.scroll_area.setWidget(self.scroll_content)
        root_layout.addWidget(self.scroll_area)

        self._build_timer_settings()
        self._build_monitor_settings()
        self._build_always_close_settings()
        self._build_minimize_settings()
        self._build_log_area()

        self.tabs.addTab(self.tab_settings, "设置·监管")

        # Tab 2: 统计
        self.stats_panel = StatsPanel(self._ctrl)
        self.tabs.addTab(self.stats_panel, "统计")
        self.tabs.setCurrentIndex(0)

        root.addWidget(self.settings_panel, 1)

    def _build_timer_settings(self):
        """番茄钟设置分组。"""
        grp = QGroupBox("番茄钟设置")
        v = QVBoxLayout(grp)
        v.setSpacing(11)

        row1 = QHBoxLayout()
        self.spin_work = self._spin(row1, "工作时长(分)", 0.1, 240, 1, 25.0)
        self.spin_short = self._spin(row1, "短休息(分)", 0.1, 120, 1, 5.0)
        row1.addStretch(1)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        self.spin_long = self._spin(row2, "长休息(分)", 0.1, 120, 1, 15.0)
        self.spin_cycles = QSpinBox()
        self.spin_cycles.setRange(1, 20)
        self.spin_cycles.setValue(4)
        self.spin_cycles.setSuffix(" 个")
        self.spin_cycles.valueChanged.connect(
            lambda v: self._ctrl.update_setting("timer", {
                "work_min": self.spin_work.value(),
                "short_min": self.spin_short.value(),
                "long_min": self.spin_long.value(),
                "cycles": v,
            }))
        row2.addWidget(self._lbl("长休息间隔"), 0)
        row2.addWidget(self.spin_cycles, 1)
        row2.addStretch(1)
        v.addLayout(row2)

        self.chk_autostart = QCheckBox("阶段结束后自动开始下一阶段")
        self.chk_autostart.setChecked(True)
        self.chk_force_break = QCheckBox("进入休息时强制关闭监管应用")
        self.chk_topmost = QCheckBox("窗口总在最前")
        self.chk_sound = QCheckBox("状态切换提示音")
        self.chk_rest_overlay = QCheckBox("休息时全屏遮罩提醒(看向窗外/眨眼)")
        self.chk_presence = QCheckBox("摄像头在场检测(离开自动暂停/休息无人关遮罩)")
        self.chk_sound.setChecked(True)
        self.chk_rest_overlay.setChecked(True)
        self.chk_presence.setChecked(True)
        self.chk_autostart.toggled.connect(lambda v: self._ctrl.update_setting("autostart", v))
        self.chk_force_break.toggled.connect(lambda v: self._ctrl.update_setting("force_on_break", v))
        self.chk_topmost.toggled.connect(self._on_topmost_toggled)
        self.chk_sound.toggled.connect(lambda v: self._ctrl.update_setting("sound_enabled", v))
        self.chk_rest_overlay.toggled.connect(self._ctrl.toggle_rest_overlay)
        self.chk_presence.toggled.connect(self._ctrl.toggle_presence)
        for chk in (self.chk_autostart, self.chk_force_break, self.chk_topmost,
                    self.chk_sound, self.chk_rest_overlay, self.chk_presence):
            v.addWidget(chk)

        row4 = QHBoxLayout()
        row4.addWidget(self._lbl("专注离开宽限(秒)"))
        self.spin_presence_grace = QDoubleSpinBox()
        self.spin_presence_grace.setRange(5.0, 120.0)
        self.spin_presence_grace.setSingleStep(5.0)
        self.spin_presence_grace.setDecimals(0)
        self.spin_presence_grace.setValue(15.0)
        self.spin_presence_grace.valueChanged.connect(
            lambda v: self._ctrl.update_setting("presence_grace_sec", v))
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
        self.scroll_layout.addWidget(grp)

    def _build_monitor_settings(self):
        """应用监管分组。"""
        grp = QGroupBox("应用监管")
        v = QVBoxLayout(grp)

        mrow1 = QHBoxLayout()
        self.chk_monitor = QCheckBox("启用监管")
        self.chk_monitor.setChecked(True)
        self.chk_monitor.toggled.connect(self._ctrl.toggle_guard)
        mrow1.addWidget(self.chk_monitor)
        mrow1.addStretch(1)
        v.addLayout(mrow1)

        mrow3 = QHBoxLayout()
        mrow3.addWidget(self._lbl("检测间隔(秒)"))
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.5, 60.0)
        self.spin_interval.setSingleStep(0.5)
        self.spin_interval.setDecimals(1)
        self.spin_interval.setValue(2.0)
        self.spin_interval.valueChanged.connect(
            lambda v: self._ctrl.update_setting("interval", v))
        mrow3.addWidget(self.spin_interval)
        mrow3.addStretch(1)
        v.addLayout(mrow3)

        mrow4 = QHBoxLayout()
        self.edit_app = QLineEdit()
        self.edit_app.setPlaceholderText("程序名，如 chrome 或 notepad.exe，回车添加")
        self.edit_app.returnPressed.connect(self._add_monitored_app)
        mrow4.addWidget(self.edit_app, 1)
        self.btn_add = QPushButton("添加")
        self.btn_del = QPushButton("删除选中")
        self.btn_clear = QPushButton("清空")
        self.btn_add.clicked.connect(self._add_monitored_app)
        self.btn_del.clicked.connect(self._remove_monitored_apps)
        self.btn_clear.clicked.connect(self._ctrl.clear_monitored_apps)
        mrow4.addWidget(self.btn_add)
        mrow4.addWidget(self.btn_del)
        mrow4.addWidget(self.btn_clear)
        v.addLayout(mrow4)

        self.list_apps = QListWidget()
        self.list_apps.setMaximumHeight(110)
        v.addWidget(self.list_apps)
        self.scroll_layout.addWidget(grp)

    def _build_always_close_settings(self):
        """全程应用自动关闭分组。"""
        grp = QGroupBox("全程应用自动关闭")
        v2 = QVBoxLayout(grp)

        arow1 = QHBoxLayout()
        self.chk_always_close = QCheckBox("启用全程自动关闭")
        self.chk_always_close.setChecked(False)
        self.chk_always_close.toggled.connect(self._ctrl.toggle_always_close)
        arow1.addWidget(self.chk_always_close)
        arow1.addStretch(1)
        v2.addLayout(arow1)

        arow2 = QHBoxLayout()
        lbl_hint = QLabel("以下应用从番茄钟启动到结束全程强制关闭（不受阶段限制）")
        lbl_hint.setStyleSheet("color: #9d9d9d; font-size: 11px;")
        arow2.addWidget(lbl_hint)
        arow2.addStretch(1)
        v2.addLayout(arow2)

        arow3 = QHBoxLayout()
        self.edit_always_app = QLineEdit()
        self.edit_always_app.setPlaceholderText("程序名，如 chrome 或 notepad.exe，回车添加")
        self.edit_always_app.returnPressed.connect(self._add_always_app)
        arow3.addWidget(self.edit_always_app, 1)
        self.btn_add_always = QPushButton("添加")
        self.btn_del_always = QPushButton("删除选中")
        self.btn_clear_always = QPushButton("清空")
        self.btn_add_always.clicked.connect(self._add_always_app)
        self.btn_del_always.clicked.connect(self._remove_always_apps)
        self.btn_clear_always.clicked.connect(self._ctrl.clear_always_apps)
        arow3.addWidget(self.btn_add_always)
        arow3.addWidget(self.btn_del_always)
        arow3.addWidget(self.btn_clear_always)
        v2.addLayout(arow3)

        self.list_always_apps = QListWidget()
        self.list_always_apps.setMaximumHeight(110)
        v2.addWidget(self.list_always_apps)
        self.scroll_layout.addWidget(grp)

    def _build_minimize_settings(self):
        """工作时段最小化应用分组。"""
        grp = QGroupBox("工作时段最小化应用")
        v3 = QVBoxLayout(grp)

        nrow1 = QHBoxLayout()
        self.chk_minimize = QCheckBox("启用工作时段最小化")
        self.chk_minimize.setChecked(False)
        self.chk_minimize.toggled.connect(self._ctrl.toggle_minimize)
        nrow1.addWidget(self.chk_minimize)
        nrow1.addStretch(1)
        v3.addLayout(nrow1)

        nrow2 = QHBoxLayout()
        lbl_hint = QLabel(
            "以下应用在工作阶段计时运行中（非暂停）会被自动最小化，"
            "固定每 5 秒检测一次；用户手动恢复窗口后会被再次最小化；"
            "本程序自身窗口不受影响")
        lbl_hint.setStyleSheet("color: #9d9d9d; font-size: 11px;")
        lbl_hint.setWordWrap(True)
        nrow2.addWidget(lbl_hint)
        nrow2.addStretch(1)
        v3.addLayout(nrow2)

        nrow3 = QHBoxLayout()
        self.edit_minimize_app = QLineEdit()
        self.edit_minimize_app.setPlaceholderText("程序名，如 chrome 或 notepad.exe，回车添加")
        self.edit_minimize_app.returnPressed.connect(self._add_minimize_app)
        nrow3.addWidget(self.edit_minimize_app, 1)
        self.btn_add_minimize = QPushButton("添加")
        self.btn_del_minimize = QPushButton("删除选中")
        self.btn_clear_minimize = QPushButton("清空")
        self.btn_add_minimize.clicked.connect(self._add_minimize_app)
        self.btn_del_minimize.clicked.connect(self._remove_minimize_apps)
        self.btn_clear_minimize.clicked.connect(self._ctrl.clear_minimize_apps)
        nrow3.addWidget(self.btn_add_minimize)
        nrow3.addWidget(self.btn_del_minimize)
        nrow3.addWidget(self.btn_clear_minimize)
        v3.addLayout(nrow3)

        self.list_minimize_apps = QListWidget()
        self.list_minimize_apps.setMaximumHeight(110)
        v3.addWidget(self.list_minimize_apps)
        self.scroll_layout.addWidget(grp)

    def _build_log_area(self):
        """日志分组。"""
        grp = QGroupBox("日志")
        v = QVBoxLayout(grp)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(2000)
        v.addWidget(self.log_text)
        self.scroll_layout.addWidget(grp, 1)

    def _lbl(self, text):
        return QLabel(text)

    def _spin(self, layout, label, lo, hi, step, default):
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
        """时长变化时保存配置（应用设置按钮才真正生效）。"""
        self._save_timer_config()

    # =====================================================================
    #  事件处理（调用 controller）
    # =====================================================================

    def _on_pause_clicked(self):
        """暂停/继续按钮。"""
        if self._ctrl.engine_state() == "running":
            self._show_async_confirm(
                "暂停确认",
                "确定要暂停当前番茄钟吗？",
                self._ctrl.request_pause
            )
        elif self._ctrl.engine_state() == "paused":
            self._ctrl.resume_timer()

    def _on_skip_clicked(self):
        """跳过按钮。"""
        if self._ctrl.engine_state() == "idle":
            self._ctrl.start_timer()
            return
        self._show_async_confirm(
            "跳过确认",
            "确定要跳过当前阶段吗？",
            self._ctrl.skip_stage
        )

    def _show_async_confirm(self, title, text, callback):
        """弹出非阻塞确认对话框。"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        msg_box.setWindowIcon(self.windowIcon())

        def on_finished(result):
            if result == QMessageBox.Yes:
                callback()

        msg_box.finished.connect(on_finished)
        msg_box.open()

    def _apply_settings(self):
        """应用番茄钟设置。"""
        try:
            self._ctrl.apply_timer_settings(
                self.spin_work.value(), self.spin_short.value(),
                self.spin_long.value(), self.spin_cycles.value())
        except (ValueError, TypeError):
            QMessageBox.warning(self, "设置无效", "请检查时长设置是否为有效数字。")
            return
        self._update_display()

    def _on_topmost_toggled(self, checked):
        """窗口总在最前勾选。"""
        if self._topmost_forced:
            self._ctrl.update_setting("topmost", checked)
            return
        self.setWindowFlag(Qt.WindowStaysOnTopHint, checked)
        self.show()
        self._ctrl.update_setting("topmost", checked)

    def _add_monitored_app(self):
        name = self.edit_app.text().strip()
        if not name:
            return
        exe = normalize_exe(name)
        if self._ctrl.add_monitored_app(exe):
            self.list_apps.addItem(exe)
        self.edit_app.clear()

    def _remove_monitored_apps(self):
        items = list(self.list_apps.selectedItems())
        if not items:
            return
        exes = [item.text() for item in items]
        self._ctrl.remove_monitored_apps(exes)
        for item in reversed(items):
            self.list_apps.takeItem(self.list_apps.row(item))

    def _add_always_app(self):
        name = self.edit_always_app.text().strip()
        if not name:
            return
        exe = normalize_exe(name)
        if self._ctrl.add_always_app(exe):
            self.list_always_apps.addItem(exe)
        self.edit_always_app.clear()

    def _remove_always_apps(self):
        items = list(self.list_always_apps.selectedItems())
        if not items:
            return
        exes = [item.text() for item in items]
        self._ctrl.remove_always_apps(exes)
        for item in reversed(items):
            self.list_always_apps.takeItem(self.list_always_apps.row(item))

    def _add_minimize_app(self):
        name = self.edit_minimize_app.text().strip()
        if not name:
            return
        exe = normalize_exe(name)
        if self._ctrl.add_minimize_app(exe):
            self.list_minimize_apps.addItem(exe)
        self.edit_minimize_app.clear()

    def _remove_minimize_apps(self):
        items = list(self.list_minimize_apps.selectedItems())
        if not items:
            return
        exes = [item.text() for item in items]
        self._ctrl.remove_minimize_apps(exes)
        for item in reversed(items):
            self.list_minimize_apps.takeItem(self.list_minimize_apps.row(item))

    def _relaunch_admin(self):
        """以管理员身份重启。"""
        if QMessageBox.question(
                self, "以管理员身份重启",
                "将关闭当前窗口并触发 UAC 提权重启。\n"
                "提权后需重新添加监管目标。是否继续？") != QMessageBox.Yes:
            return
        if getattr(sys, "frozen", False):
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
        self._ctrl.on_quit()
        QApplication.instance().quit()

    # =====================================================================
    #  UI 更新（由 controller 信号触发）
    # =====================================================================

    def _update_display(self):
        """更新倒计时、状态栏、按钮状态。"""
        ctrl = self._ctrl
        self.lbl_time.setText(self._fmt_remaining())
        phase = ctrl.engine_phase()
        self.lbl_phase.setText(PHASE_NAMES[phase])
        self.lbl_phase.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {DARK_PHASE_COLORS[phase]};")

        state = STATE_NAMES[ctrl.engine_state()]
        self.lbl_status.setText(
            f"本轮番茄: {ctrl.engine_work_count()}/{ctrl.engine_cycles()}   状态: {state}   "
            f"{ctrl.monitor_status_text()}   {ctrl.presence_status()}")

        self.btn_start.setEnabled(ctrl.engine_state() == "idle")
        self.btn_pause.setEnabled(ctrl.engine_state() != "idle")
        self.btn_pause.setText("继续" if ctrl.engine_state() == "paused" else "暂停")

    def _on_phase_changed(self, new_phase):
        """阶段切换时播放提示音。"""
        play_state_sound(new_phase, self.chk_sound.isChecked())
        if self.isVisible():
            self._flash_window()

    def _append_log(self, text):
        """追加日志。"""
        ts = time.strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{ts}] {text}")

    def _handle_overlay_request(self, show, overlay_type):
        """处理遮罩显示/隐藏请求。"""
        if show:
            self._show_overlay(overlay_type)
        else:
            self._hide_overlay()

    def _show_presence_alert(self, message):
        """显示摄像头降级提示。"""
        self._append_log(f"! {message}")

    def _fmt_remaining(self):
        """剩余秒数 → "MM:SS"。"""
        seconds = self._ctrl.remaining_seconds()
        remaining = int(seconds + 0.999)
        mm, ss = divmod(remaining, 60)
        return f"{mm:02d}:{ss:02d}"

    # =====================================================================
    #  窗口显示模式
    # =====================================================================

    def _set_compact_mode(self, compact, move_corner=False):
        """切换收起/展开模式。"""
        if compact == self._compact:
            if compact and move_corner:
                self._move_top_right()
            return
        if compact:
            self._expanded_size = self.size()
            self._compact = True
            self.settings_panel.hide()
            self.setMinimumSize(COMPACT_W, COMPACT_H)
            self.resize(self.width(), COMPACT_H)
            self.btn_expand.setText("展开面板 ▾")
            if move_corner:
                self._move_top_right()
        else:
            self._compact = False
            self.settings_panel.show()
            self.tabs.setCurrentIndex(1)
            self.setMinimumSize(EXPANDED_MIN_W, EXPANDED_MIN_H)
            target = self._expanded_size or QSize(EXPANDED_MIN_W, EXPANDED_MIN_H)
            self.resize(target)
            self.btn_expand.setText("收起面板 ▾")
            avail = self.screen().availableGeometry()
            if self.y() + self.height() > avail.bottom():
                self.move(self.x(), max(avail.top(), avail.bottom() - self.height()))

    def _toggle_compact(self):
        """用户手动切换收起/展开。"""
        self._set_compact_mode(not self._compact)
        self._append_log("已切换为" + ("收起" if self._compact else "展开") + "模式")

    def _move_top_right(self):
        """移到屏幕右上角。"""
        avail = self.screen().availableGeometry()
        x = max(avail.x(), avail.right() - self.width() - TOP_RIGHT_MARGIN)
        self.move(x, avail.top() + TOP_RIGHT_MARGIN)

    # =====================================================================
    #  休息遮罩
    # =====================================================================

    def _show_overlay(self, overlay_type):
        """显示休息遮罩。"""
        if not self.chk_rest_overlay.isChecked():
            return
        if self._overlay is not None:
            if getattr(self._overlay, '_overlay_type', None) != overlay_type:
                self._overlay.close()
                self._overlay = None
        if self._overlay is None:
            self._overlay = RestOverlay(overlay_type)
        self._overlay.show_overlay()
        if not self.isVisible():
            self.showNormal()
        if not self.chk_topmost.isChecked():
            self._topmost_forced = True
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()
        self.raise_()
        self.activateWindow()
        self._avoid_overlay_text()
        # 进入休息时自动收起
        self._set_compact_mode(True, move_corner=True)
        self._append_log("休息模式：窗口已收起至右上角")

    def _hide_overlay(self):
        """隐藏休息遮罩。"""
        if self._overlay is not None and self._overlay.isVisible():
            self._overlay.hide()
        if self._topmost_forced:
            self._topmost_forced = False
            self.setWindowFlag(Qt.WindowStaysOnTopHint, self.chk_topmost.isChecked())
            self.show()

    def _avoid_overlay_text(self):
        """主窗口避免遮挡遮罩文本。"""
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

    # =====================================================================
    #  配置加载/保存
    # =====================================================================

    def _load_config_to_ui(self):
        """从 controller 配置加载到 UI。

        加载期间对全部相关控件 blockSignals：防止 setChecked/setValue 触发
        已连接的控制器回调（重复写盘、误触"监管已关闭"等日志、构造期间过早
        show() 等），加载完成后统一恢复。
        所有配置值做类型防御：旧版/手改的异常类型（字符串数字、"false"、
        非 dict 的 timer 等）一律回退默认值，避免 setValue/setChecked 抛
        异常导致启动崩溃。
        """
        def _num(v, default):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        def _int(v, default):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return default

        def _bool(v, default):
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                s = v.strip().lower()
                if s in ("1", "true", "yes", "on"):
                    return True
                if s in ("0", "false", "no", "off"):
                    return False
            return bool(default)

        def _list(v):
            # 仅接受列表且只保留字符串元素，过滤掉异常类型（如 int/None）
            if not isinstance(v, list):
                return []
            return [x for x in v if isinstance(x, str)]

        cfg = self._ctrl.config
        widgets = (self.spin_work, self.spin_short, self.spin_long,
                   self.spin_cycles, self.spin_interval, self.spin_presence_grace,
                   self.chk_monitor, self.chk_always_close, self.chk_minimize,
                   self.chk_force_break, self.chk_autostart, self.chk_topmost,
                   self.chk_sound, self.chk_rest_overlay, self.chk_presence)
        for w in widgets:
            w.blockSignals(True)
        try:
            timer = cfg.get("timer", {})
            if not isinstance(timer, dict):
                timer = {}
            self.spin_work.setValue(_num(timer.get("work_min"), 25))
            self.spin_short.setValue(_num(timer.get("short_min"), 5))
            self.spin_long.setValue(_num(timer.get("long_min"), 15))
            self.spin_cycles.setValue(_int(timer.get("cycles"), 4))
            self.spin_interval.setValue(_num(cfg.get("interval"), 2.0))
            self.chk_monitor.setChecked(_bool(cfg.get("monitor_enabled"), True))
            self.chk_always_close.setChecked(
                _bool(cfg.get("always_close_enabled"), False))
            self.chk_minimize.setChecked(_bool(cfg.get("minimize_enabled"), False))
            self.chk_force_break.setChecked(_bool(cfg.get("force_on_break"), False))
            self.chk_autostart.setChecked(_bool(cfg.get("autostart"), True))
            self.chk_topmost.setChecked(_bool(cfg.get("topmost"), False))
            self.chk_sound.setChecked(_bool(cfg.get("sound_enabled"), True))
            self.chk_rest_overlay.setChecked(_bool(cfg.get("rest_overlay"), True))
            self.chk_presence.setChecked(_bool(cfg.get("presence_enabled"), True))
            self.spin_presence_grace.setValue(
                _num(cfg.get("presence_grace_sec"), 15.0))
        finally:
            for w in widgets:
                w.blockSignals(False)
        if self.chk_topmost.isChecked():
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()
        monitored = _list(cfg.get("monitored"))
        for exe in monitored:
            self.list_apps.addItem(exe)
        if monitored:
            self._append_log(f"已从设置文件加载 {len(monitored)} 个监管目标")
        always = _list(cfg.get("always_closed"))
        for exe in always:
            self.list_always_apps.addItem(exe)
        if always:
            self._append_log(f"已从设置文件加载 {len(always)} 个全程关闭目标")
        minimized = _list(cfg.get("work_minimized"))
        for exe in minimized:
            self.list_minimize_apps.addItem(exe)
        if minimized:
            self._append_log(f"已从设置文件加载 {len(minimized)} 个工作最小化目标")
        self._update_display()

    def _save_timer_config(self):
        """保存番茄钟时长配置。"""
        self._ctrl.update_setting("timer", {
            "work_min": self.spin_work.value(),
            "short_min": self.spin_short.value(),
            "long_min": self.spin_long.value(),
            "cycles": self.spin_cycles.value(),
        })

    # =====================================================================
    #  云母背景
    # =====================================================================

    def _reapply_mica(self):
        if self._mica_applied and self._mica_supported:
            try:
                apply_mica(int(self.winId()))
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        if not self._mica_supported:
            return
        if apply_mica(int(self.winId())):
            self._mica_applied = True
            if not self._mica_logged:
                self._mica_logged = True
                self._append_log("已启用 Windows 11 云母背景效果")
        elif not self._mica_applied:
            self._mica_supported = False
            self.setAttribute(Qt.WA_TranslucentBackground, False)
            QApplication.instance().setStyleSheet(QSS)
            self._append_log("当前系统不支持云母，使用纯深色背景")

    def _flash_window(self):
        """阶段切换时闪烁提示。"""
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.show()
        QTimer.singleShot(600, self._restore_topmost)

    def _restore_topmost(self):
        want = self._topmost_forced or self.chk_topmost.isChecked()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, want)
        self.show()

    # =====================================================================
    #  窗口事件
    # =====================================================================

    def closeEvent(self, event):
        """关闭按钮 = 隐藏到托盘；托盘不可用时 = 确认退出（需走完整清理）。"""
        if self._tray is not None and TrayIcon.isSystemTrayAvailable():
            self._tray.show_hint("已最小化到托盘，点击托盘图标恢复；右键托盘图标可退出")
            self.hide()
            event.ignore()
        elif QMessageBox.question(self, "退出", "确定要退出番茄钟·应用监管吗？") == QMessageBox.Yes:
            # 无托盘时"关闭=退出"：必须走 on_quit 完成落库/释放摄像头/停扫描器，
            # 否则当前工作段统计丢失、摄像头句柄不释放
            self._ctrl.on_quit()
            event.accept()
        else:
            event.ignore()

    def changeEvent(self, event):
        """最小化时自动隐藏到托盘。"""
        if event.type() == QEvent.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)
