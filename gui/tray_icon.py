# -*- coding: utf-8 -*-
"""系统托盘 (TrayIcon) — 纯 GUI 组件。

提供系统托盘图标、悬浮提示和右键菜单。
只通过依赖注入接收 controller，不直接访问核心层。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from core.utils import PHASE_NAMES


# 引擎状态 → 界面文案（状态栏/托盘菜单共用）
STATE_NAMES = {"running": "进行中", "paused": "已暂停", "idle": "待开始"}

# 应用标题
APP_TITLE = "番茄钟 · 应用监管"


def make_tray_icon_pixmap():
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
    return pm


def make_tray_icon():
    """生成托盘图标 QIcon。"""
    return QIcon(make_tray_icon_pixmap())


class TrayIcon(QSystemTrayIcon):
    """系统托盘图标：左键切换主窗口；右键弹出精简状态菜单。
    
    通过 controller 获取状态信息，通过 main_window 引用操作主窗口。
    """

    def __init__(self, controller, main_window, parent=None):
        """初始化托盘图标。
        
        Args:
            controller: ApplicationController 实例
            main_window: MainWindow 实例（用于显示/隐藏）
        """
        icon = make_tray_icon()
        super().__init__(icon, parent)
        
        self._controller = controller
        self._main_window = main_window
        self._hint_shown = False
        
        self.setToolTip(APP_TITLE)
        self.activated.connect(self._on_activated)
        self.show()
        
        # 连接 controller 信号以更新悬浮提示
        self._controller.tick_updated.connect(self._update_tooltip)
        
        # 检查托盘可用性
        if not QSystemTrayIcon.isSystemTrayAvailable():
            controller.log_appended.emit("! 系统托盘不可用，关闭窗口时将直接退出")

    def _on_activated(self, reason):
        """托盘点击：左键切换主窗口；右键弹出精简状态菜单。

        注意只处理 Trigger(单击左键)：部分平台双击会同时发 Trigger 和
        DoubleClick，若两者都处理会连续开关两次等于没反应。
        """
        if reason == QSystemTrayIcon.Trigger:
            self._toggle_window()
        elif reason == QSystemTrayIcon.Context:
            self._show_menu()

    def _toggle_window(self):
        """切换主窗口显示/隐藏。"""
        win = self._main_window
        if win.isVisible():
            win.hide()  # 从任务栏消失，后台运行
        else:
            win.showNormal()
            win.raise_()
            win.activateWindow()

    def _show_menu(self):
        """在光标处弹出托盘菜单（每次现构建，保证状态最新）。"""
        self._build_menu().exec(QCursor.pos())

    def _build_menu(self):
        """构建精简状态菜单（可独立测试，不含 exec）。"""
        ctrl = self._controller
        state = STATE_NAMES[ctrl.engine_state()]

        menu = QMenu(self._main_window)
        for text in (f"阶段: {PHASE_NAMES[ctrl.engine_phase()]} · {state}",
                     f"剩余: {self._fmt_remaining()}",
                     f"本轮番茄: {ctrl.engine_work_count()}/{ctrl.engine_cycles()}",
                     f"监管: {ctrl.monitor_status_text()}"):
            act = QAction(text, menu)
            act.setEnabled(False)  # 只读状态行
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction("显示/隐藏主窗口", self._toggle_window)
        if ctrl.engine_state() == "idle":
            menu.addAction("开始", ctrl.start_timer)
        else:
            menu.addAction("暂停/继续", self._on_pause_resume)
        menu.addAction("跳过", self._on_skip_with_confirm)
        menu.addAction("重置", ctrl.reset_timer)
        menu.addSeparator()
        menu.addAction("退出", self._quit_app)
        return menu

    def _on_pause_resume(self):
        """暂停/继续按钮处理。"""
        ctrl = self._controller
        if ctrl.engine_state() == "paused":
            ctrl.resume_timer()
        else:
            # 异步确认暂停
            self._show_async_confirm(
                "暂停确认",
                "确定要暂停当前番茄钟吗？",
                ctrl.request_pause
            )

    def _on_skip_with_confirm(self):
        """跳过按钮（带确认）。"""
        ctrl = self._controller
        if ctrl.engine_state() == "idle":
            ctrl.start_timer()
            return
        self._show_async_confirm(
            "跳过确认",
            "确定要跳过当前阶段吗？",
            ctrl.skip_stage
        )

    def _show_async_confirm(self, title, text, callback):
        """弹出非阻塞确认对话框。"""
        msg_box = QMessageBox(self._main_window)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        msg_box.setWindowIcon(self._main_window.windowIcon())

        def on_finished(result):
            if result == QMessageBox.Yes:
                callback()

        msg_box.finished.connect(on_finished)
        msg_box.open()

    def _quit_app(self):
        """真正退出程序（托盘菜单专用）。"""
        # 先关闭全屏遮罩（检查点/重启/休息），防止遮罩挡住退出确认框
        self._main_window.dismiss_overlays()
        # 二次确认提示
        msg_box = QMessageBox(self._main_window)
        msg_box.setWindowTitle("退出确认")
        msg_box.setText("确定要退出番茄钟·应用监管吗？")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        msg_box.setWindowIcon(self._main_window.windowIcon())
        reply = msg_box.exec()
        if reply != QMessageBox.Yes:
            return
        self._controller.on_quit()
        self.hide()
        QApplication.instance().quit()

    def _fmt_remaining(self):
        """剩余秒数 → "MM:SS"（向上取整）。"""
        seconds = self._controller.remaining_seconds()
        remaining = int(seconds + 0.999)
        mm, ss = divmod(remaining, 60)
        return f"{mm:02d}:{ss:02d}"

    def _update_tooltip(self):
        """更新托盘悬浮提示：阶段 + 剩余时间。"""
        ctrl = self._controller
        self.setToolTip(
            f"{APP_TITLE}\n{PHASE_NAMES[ctrl.engine_phase()]} · "
            f"{self._fmt_remaining()} 剩余")

    def show_hint(self, message):
        """显示首次使用提示。"""
        if not self._hint_shown:
            self._hint_shown = True
            self._controller.log_appended.emit(message)
