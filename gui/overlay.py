# -*- coding: utf-8 -*-
"""休息全屏遮罩 (RestOverlay) — 纯 GUI 组件。

进入休息阶段时强制全屏覆盖（无边框/置顶/覆盖所有屏幕/拦截鼠标），
提醒用户放松双眼。只响应 controller 的 overlay_requested 信号。

多屏实现：为每个 QScreen 创建独立的遮罩窗口（_MultiScreenOverlay 基类），
相比"单窗口 + 所有屏幕联合矩形"，每屏独立窗口可保证每个屏幕都被完整覆盖，
规避负坐标副屏、DPI 差异、错位布局下的渲染不完整问题。
"""

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import (QApplication, QLabel, QVBoxLayout, QWidget)


# 遮罩显示文本
REST_OVERLAY_TEXT_SHORT = "请立即看向6米以外的窗外物体，并眨眼10次！"
REST_OVERLAY_TEXT_LONG = "请立即看向6米以外的窗外物体，并眨眼10次！\n\n请拿起水杯到厨房倒一杯水"


class _OverlayScreen(QWidget):
    """单个屏幕的遮罩窗口：无边框置顶、拦截鼠标/键盘、禁止被关闭。"""

    def __init__(self, title=""):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle(title)
        self.setStyleSheet("background: rgba(10, 10, 14, 235);")

    def keyPressEvent(self, event):
        # 遮罩不响应任何按键（防 Esc 等绕过）
        event.ignore()

    def mousePressEvent(self, event):
        # 拦截点击，防止穿透到下层程序（"强制"遮罩）
        event.accept()

    def closeEvent(self, event):
        # 禁止被关闭（Alt+F4 / 系统关闭均忽略），只能由程序按阶段切换隐藏
        event.ignore()


class _MultiScreenOverlay(QWidget):
    """多屏全屏遮罩基类：为每个屏幕创建独立的遮罩窗口。

    子类只需实现 _build_content(window, geometry) 向每个窗口填充内容。
    对外提供 show_overlay()/show()/hide()/close()/isVisible()/deleteLater()，
    统一代理到所有子窗口，保持与单窗口遮罩相同的调用接口。
    """

    def __init__(self, title=""):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._windows = []   # 每个屏幕一个遮罩窗口
        self._title = title
        self._build_windows()

    def _build_windows(self):
        """为每个屏幕创建独立遮罩窗口（创建时按当前屏幕枚举）。"""
        for w in self._windows:
            w.deleteLater()
        self._windows = []
        for screen in QApplication.screens():
            w = _OverlayScreen(self._title)
            w.setGeometry(screen.geometry())
            self._build_content(w, screen.geometry())
            self._windows.append(w)

    def _build_content(self, window, geometry):
        """子类实现：向单个屏幕窗口填充内容。"""
        raise NotImplementedError

    # ---- 显示 / 隐藏（代理到所有子窗口） -------------------------------

    def show_overlay(self):
        """全屏显示并置顶；不激活、不抢键盘焦点。刷新各窗口几何以跟随屏幕布局。"""
        screens = QApplication.screens()
        for i, w in enumerate(self._windows):
            if i < len(screens):
                w.setGeometry(screens[i].geometry())
            w.show()
            w.raise_()

    def show(self):
        for w in self._windows:
            w.show()

    def hide(self):
        for w in self._windows:
            w.hide()

    def close(self):
        for w in self._windows:
            w.close()

    def isVisible(self):
        return any(w.isVisible() for w in self._windows)

    def deleteLater(self):
        for w in self._windows:
            w.deleteLater()
        self._windows = []
        super().deleteLater()

    @staticmethod
    def _virtual_geometry():
        """所有屏幕的联合矩形（供主窗口避让遮罩文本的中心区域计算）。"""
        screens = QApplication.screens()
        if not screens:
            return QRect(0, 0, 1920, 1080)
        rect = screens[0].geometry()
        for s in screens[1:]:
            rect = rect.united(s.geometry())
        return rect


class RestOverlay(_MultiScreenOverlay):
    """休息全屏遮罩：每个屏幕独立窗口，中央显示护眼提醒文本。

    支持短休息和长休息两种模式，长休息额外显示喝水提示。
    """

    def __init__(self, overlay_type="short"):
        """初始化遮罩。

        Args:
            overlay_type: "short" 短休息 或 "long" 长休息
        """
        self._overlay_type = overlay_type
        super().__init__(title="休息提醒")

    def _build_content(self, window, geometry):
        lay = QVBoxLayout(window)
        lay.setContentsMargins(40, 40, 40, 40)

        # 根据休息类型选择文本（字号按当前屏幕宽度自适应）
        if self._overlay_type == "long":
            text = REST_OVERLAY_TEXT_LONG
            px = max(24, min(48, int(geometry.width() / 20)))  # 长休息文字稍小以容纳更多内容
        else:
            text = REST_OVERLAY_TEXT_SHORT
            px = max(28, min(56, int(geometry.width() / 18)))

        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
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
