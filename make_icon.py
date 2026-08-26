# -*- coding: utf-8 -*-
"""生成 exe 用的番茄图标 icon.ico（复用托盘图标的绘制逻辑）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication

from gui.tray_icon import make_tray_icon_pixmap

app = QApplication([])
# 以 256x256 渲染，平滑放大绘制的 64x64 番茄图标
pm = make_tray_icon_pixmap()
pm = pm.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
if pm.save(out):
    print(f"icon saved: {out}")
else:
    print("icon save FAILED")
    sys.exit(1)
