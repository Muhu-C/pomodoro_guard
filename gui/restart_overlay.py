# -*- coding: utf-8 -*-
"""重启全屏遮罩 (RestartOverlay) — 纯 GUI 组件。

检查点 SOS 后进入"重启"阶段时，先显示 1 分钟遮罩（固定时长，不受
摄像头等其他因素影响），文案鼓励用户不要为无长远意义的事情内耗。

多屏实现：继承 _MultiScreenOverlay，为每个 QScreen 创建独立遮罩窗口，
保证每个屏幕都被完整覆盖（规避负坐标副屏/DPI 差异/错位布局的渲染问题）。
只能由控制器按重启阶段推进（1 分钟后）隐藏。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout

from gui.overlay import _MultiScreenOverlay


# 重启遮罩文案
RESTART_OVERLAY_TEXT = "不要为了无长远意义的事情折磨自己\n\n你还有时间"


class RestartOverlay(_MultiScreenOverlay):
    """重启 1 分钟遮罩：每个屏幕独立窗口，显示固定文案。"""

    def __init__(self):
        super().__init__(title="重启")

    def _build_content(self, window, geometry):
        lay = QVBoxLayout(window)
        lay.setContentsMargins(40, 40, 40, 40)

        px = max(24, min(48, int(geometry.width() / 22)))

        label = QLabel(RESTART_OVERLAY_TEXT)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: #f39c12; font-size: {px}px; font-weight: bold; "
            "background: transparent;")
        lay.addStretch(1)
        lay.addWidget(label)

        hint = QLabel("重启阶段 · 1 分钟后开始 5 分钟重启工作")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            "color: #8a8a8a; font-size: 13px; background: transparent;")
        lay.addWidget(hint)
        lay.addStretch(1)
