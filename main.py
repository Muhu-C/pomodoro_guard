# -*- coding: utf-8 -*-
"""程序入口 (main.py) — 所有模块的实例化、依赖注入与组装。

组装顺序：
    1. QApplication + 单例锁（第二实例弹提示退出，崩溃残留锁自动清理）
    2. core/* 核心层（无 GUI 依赖）
    3. controller 总控制器（注入 core 实例）
    4. gui 主窗口（注入 controller；内部自建系统托盘）

退出清理：由托盘菜单「退出」/「以管理员身份重启」路径调用
controller.on_quit() 完成（落库 + 释放摄像头 + 停止扫描器线程），
因此这里不再连接 aboutToQuit（on_quit 中的段落落库非幂等，
重复调用会导致统计重复写入）。
"""

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from core.config import ConfigManager
from core.engine import PomodoroEngine
from core.guards import GuardianService
from core.presence import PresenceDetector
from core.stats import FocusStats
from controller import ApplicationController
from gui.main_window import MainWindow, acquire_lock


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 单例保护：已有实例运行时提示并退出（崩溃残留锁由 acquire_lock 自动清理）
    lock = acquire_lock()
    if lock is None:
        QMessageBox.information(
            None, "已在运行",
            "番茄钟·应用监管已在后台运行。\n请点击系统托盘中的番茄图标打开主窗口。")
        return 0

    # 1. 实例化核心层（无 GUI 依赖）
    config = ConfigManager()
    engine = PomodoroEngine()        # 默认值即可，controller 构造时从配置恢复时长
    stats = FocusStats()             # 默认 db 路径（脚本目录 / 打包后 exe 目录）
    presence = PresenceDetector()    # 默认参数（5s 采样、人脸+人体双模型）
    guards = GuardianService(config)  # 内部自动起三个 QThread（监管/全程关闭/最小化）

    # 2. 组装总控制器（注入所有核心对象）
    controller = ApplicationController(config, engine, presence, guards, stats)

    # 3. 组装 GUI（注入控制器；MainWindow 内部自建系统托盘）
    window = MainWindow(controller)
    window.show()
    screen = app.primaryScreen()
    if screen:
        screen_geom = screen.availableGeometry()      # 可用区域（排除任务栏）
        window_rect = window.frameGeometry()          # 窗口框架几何（含标题栏）
        margin = 25                                   # 边距
        x = screen_geom.right() - window_rect.width() - margin
        y = screen_geom.top() + margin
        window.move(x, y)

    # lock 在本函数局部保持引用，随 app.exec() 返回后释放
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
