# -*- coding: utf-8 -*-
"""休息全屏遮罩 (RestOverlay) — 纯 GUI 组件。

进入休息阶段时强制全屏覆盖（无边框/置顶/覆盖所有屏幕/拦截鼠标），
提醒用户放松双眼。只响应 controller 的 overlay_requested 信号。
"""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


# 遮罩显示文本
REST_OVERLAY_TEXT_SHORT = "请立即看向6米以外的窗外物体，并眨眼10次！"
REST_OVERLAY_TEXT_LONG = "请立即看向6米以外的窗外物体，并眨眼10次！\n\n请拿起水杯到厨房倒一杯水"


class RestOverlay(QWidget):
    """全屏强制遮罩：无边框、置顶、覆盖所有屏幕、拦截鼠标。

    中央显示护眼提醒文本；不接收焦点（键盘焦点留在主窗口），且禁止被关闭
    （closeEvent 忽略），只能由主窗口按阶段切换显示/隐藏。
    支持短休息和长休息两种模式，长休息额外显示喝水提示。
    """

    def __init__(self, overlay_type="short"):
        """初始化遮罩。
        
        Args:
            overlay_type: "short" 短休息 或 "long" 长休息
        """
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("休息提醒")
        self._overlay_type = overlay_type
        self._build()

    def _build(self):
        vg = self._virtual_geometry()
        self.setGeometry(vg)
        self.setStyleSheet("background: rgba(10, 10, 14, 235);")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 40, 40, 40)

        # 根据休息类型选择文本
        if self._overlay_type == "long":
            text = REST_OVERLAY_TEXT_LONG
            px = max(24, min(48, int(vg.width() / 20)))  # 长休息文字稍小以容纳更多内容
        else:
            text = REST_OVERLAY_TEXT_SHORT
            px = max(28, min(56, int(vg.width() / 18)))
        
        # 中央大字护眼提醒，字号按屏幕宽度自适应
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
