# -*- coding: utf-8 -*-
"""生成 exe 用的番茄图标 icon.ico（复用托盘图标的绘制逻辑）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

import pomodoro_guard as pg

app = QApplication([])
pm = pg.make_tray_icon().pixmap(256, 256)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
if pm.save(out):
    print(f"icon saved: {out}")
else:
    print("icon save FAILED")
    sys.exit(1)
