# -*- coding: utf-8 -*-
"""检查点确认全屏遮罩 (CheckpointOverlay) — 纯 GUI 组件。

检查点时间到时强制全屏覆盖：极简显示（确认文本 + 两个按钮 +
合并时的待确认数量）。10 分钟倒计时显示在主窗口上（红色"检查点" +
倒计时），不在此遮罩上。

按钮（每个屏幕的遮罩窗口上都有）：
- 「在状态」 → controller.checkpoint_ok()（恢复正常）
- 「不在状态(SOS)」 → controller.checkpoint_sos()（进入重启流程）

多屏实现：继承 _MultiScreenOverlay，为每个 QScreen 创建独立遮罩窗口，
保证每个屏幕都被完整覆盖（规避负坐标副屏/DPI 差异/错位布局的渲染问题）。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from gui.overlay import _MultiScreenOverlay


class CheckpointOverlay(_MultiScreenOverlay):
    """检查点确认遮罩：极简文本 + 两个按钮（每个屏幕一个窗口）。"""

    def __init__(self, controller, pending_count=1):
        self._controller = controller
        self._pending_count = pending_count
        super().__init__(title="检查点确认")

    def _build_content(self, window, geometry):
        # 1. 主水平布局：左右分割，不留间隙
        main_h_lay = QHBoxLayout(window)
        main_h_lay.setContentsMargins(0, 0, 0, 0)
        main_h_lay.setSpacing(0)

        # 2. 左侧 75% 的内容垂直布局（边距加大到 80，离边缘更远）
        left_lay = QVBoxLayout()
        left_lay.setContentsMargins(80, 80, 80, 80)  # 原为 48，改为 80
        left_lay.setSpacing(16)

        px = max(28, min(56, int(geometry.width() / 20)))

        title = QLabel("检查点确认")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color: #ff6b6b; font-size: {px}px; font-weight: bold; "
            "background: transparent;")
        left_lay.addStretch(2)
        left_lay.addWidget(title)

        if self._pending_count > 1:
            sub = QLabel(f"今日有 {self._pending_count} 个检查点待确认")
            sub.setAlignment(Qt.AlignCenter)
            sub.setStyleSheet(
                "color: #f5f6fa; font-size: 20px; background: transparent;")
            left_lay.addWidget(sub)

        prompt = QLabel("今天在状态吗？\n（倒计时显示在主窗口上，10 分钟超时默认视为在状态）")
        prompt.setAlignment(Qt.AlignCenter)
        prompt.setStyleSheet(
            "color: #f5f6fa; font-size: 18px; background: transparent;")
        left_lay.addWidget(prompt)

        # 3. 按钮行：左右加伸缩，使两个按钮在左侧区域内水平居中
        btn_row = QHBoxLayout()
        btn_row.setSpacing(32)

        btn_ok = QPushButton("在状态")
        btn_ok.setMinimumHeight(56)
        btn_ok.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #0a0a0e; "
            "background: #2ecc71; border: none; border-radius: 8px;")
        btn_ok.clicked.connect(self._controller.checkpoint_ok)

        btn_sos = QPushButton("不在状态(SOS)")
        btn_sos.setMinimumHeight(56)
        btn_sos.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #f5f6fa; "
            "background: #e74c3c; border: none; border-radius: 8px;")
        btn_sos.clicked.connect(self._controller.checkpoint_sos)

        # 按钮行左右加入伸缩，居中显示
        btn_row.addStretch(1)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_sos)
        btn_row.addStretch(1)

        left_lay.addLayout(btn_row)
        left_lay.addStretch(3)

        # 4. 组装主布局：左侧占 3 份（75%），右侧占 1 份空白（25%）
        main_h_lay.addLayout(left_lay, 3)
        main_h_lay.addStretch(1)  # 右侧 25% 为空