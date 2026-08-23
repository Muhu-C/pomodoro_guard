"""视图层 (只做显示与交互)。

本包包含主窗口、休息全屏遮罩、系统托盘与自绘组件。
只依赖 controller（通过依赖注入），禁止包含业务逻辑。

模块结构：
- main_window: 主窗口（UI 布局 + 信号绑定）
- overlay: 休息全屏遮罩
- tray_icon: 系统托盘图标和菜单
- widgets: 自绘组件（柱状图、统计面板）
- theme: 主题样式、云母背景、提示音工具
"""

from gui.main_window import MainWindow, acquire_lock
from gui.overlay import RestOverlay
from gui.tray_icon import TrayIcon, make_tray_icon
from gui.widgets import StatsPanel, BarChart, format_duration
from gui.theme import (QSS, QSS_MICA, DARK_PHASE_COLORS, play_state_sound,
                       supports_mica, apply_mica)

__all__ = [
    "MainWindow", "acquire_lock",
    "RestOverlay",
    "TrayIcon", "make_tray_icon",
    "StatsPanel", "BarChart", "format_duration",
    "QSS", "QSS_MICA", "DARK_PHASE_COLORS", "play_state_sound",
    "supports_mica", "apply_mica",
]