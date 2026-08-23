# -*- coding: utf-8 -*-
"""自绘组件 (widgets) — 纯 GUI 组件。

包含统计面板 (StatsPanel) 和柱状图 (BarChart)，用于展示专注时长数据。
只通过依赖注入接收 controller，不直接访问核心层。
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QToolTip, QVBoxLayout, QWidget)


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
    """「统计」页：时间档切换 + 专注总时长大数字 + streak + 柱状图 + 清除。
    
    通过 controller 获取统计数据，不直接访问核心层。
    """

    PERIODS = [("today", "今日"), ("week", "本周"), ("month", "本月"),
               ("total", "累计")]

    def __init__(self, controller, parent=None):
        """初始化统计面板。
        
        Args:
            controller: ApplicationController 实例，用于获取统计数据
        """
        super().__init__(parent)
        self._controller = controller
        self._period = "today"
        self._build()
        
        # 连接 controller 信号
        self._controller.stats_changed.connect(self.refresh)
        self._controller.tick_updated.connect(self._on_tick)

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

    def _on_tick(self):
        """200ms 心跳时刷新实时数据（仅今日档）。"""
        if self._period == "today" and self.isVisible():
            self.refresh()

    def _set_period(self, key):
        self._period = key
        self._btns[key].setChecked(True)
        self.refresh()

    def refresh(self):
        """刷新面板；从 controller 获取统计数据。"""
        ctrl = self._controller
        stats = ctrl.stats
        
        if self._period == "today":
            total = stats.today_duration() + max(0, int(ctrl.live_focus_extra()))
            self.lbl_big.setText(format_duration(total))
            self.chart.hide()
            self.lbl_empty.hide()
        elif self._period == "week":
            items = [(k[5:], v) for k, v in stats.daily_totals(7).items()]
            self._render_chart(items, "本周暂无专注记录")
        elif self._period == "month":
            items = [(k[5:], v) for k, v in stats.daily_totals(30).items()]
            self._render_chart(items, "本月暂无专注记录")
        else:  # total
            items = stats.monthly_totals()
            self._render_chart(items, "暂无专注记录")
        n = stats.streak()
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
        self._controller.clear_stats()
        self.refresh()
