# -*- coding: utf-8 -*-
"""配置读写：定位并读写 pomodoro_guard_config.json。"""

import json
import os
import sys
import threading

# 设置文件：与程序本体同目录（exe 打包后与 exe 同目录，便于用户找到）。
# 本模块位于 core/ 子包内，非冻结态下须向上回溯一层到项目根目录，
# 以保持配置文件位置与重构前一致（与 main.py / 打包后的 exe 同级）。
if getattr(sys, "frozen", False):  # PyInstaller 打包后 __file__ 指向临时目录，须用 exe 目录
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_BASE_DIR, "pomodoro_guard_config.json")


def load_config():
    """读取设置文件；不存在或损坏时返回空字典。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    """写设置文件；失败返回 False（如目录只读）。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


class ConfigManager:
    """配置管理器：内存缓存 + 即时写盘，线程安全。

    对外提供 get/set/save 接口；底层继续使用 pomodoro_guard_config.json。
    所有设置变更通过 set() 立即持久化，保证新架构下各模块读取一致。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._data = load_config()

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            save_config(self._data)

    def save(self):
        with self._lock:
            save_config(self._data)

    def reload(self):
        with self._lock:
            self._data = load_config()