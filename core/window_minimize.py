# -*- coding: utf-8 -*-
"""番茄钟 · 应用监管 —— 工作时段最小化应用（核心逻辑层，UI 无关）
===============================================================
按进程名批量最小化目标程序的顶层可见窗口。算法与示例
MinimizeWindowByProcessNameInsteadOfWindowTitle.py 一致：

1) EnumWindows 枚举所有可见顶层窗口（IsWindowVisible）；
2) GetWindowThreadProcessId 取窗口所属进程 PID，psutil 反查进程名；
3) 进程名命中目标 -> 若窗口未最小化(IsIconic=False) 则 ShowWindow(SW_MINIMIZE)，
   已最小化的跳过（幂等——用户自行恢复窗口后会被再次最小化）。

安全护栏：
- 本程序自身的窗口（同 PID）一律跳过，绝不会把自己的界面最小化；
- pywin32 或 psutil 未安装时整体降级为不可用（error 记录原因），
  其余功能不受影响，与其他可选依赖的处理方式一致。

扫描策略由 core/guards.py 调度：仅"工作 + 运行中(非暂停)"阶段、
固定 5 秒间隔，后台线程执行。
"""

import os
import time

try:
    import win32con
    import win32gui
    import win32process
    HAS_PYWIN32 = True
except ImportError:  # pywin32 未安装：功能降级为不可用
    win32con = None
    win32gui = None
    win32process = None
    HAS_PYWIN32 = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # psutil 未安装：无法反查 PID -> 进程名，功能不可用
    psutil = None
    HAS_PSUTIL = False

from .utils import normalize_exe


class WindowMinimizeManager:
    """按进程名批量最小化目标程序的顶层可见窗口（无 GUI 依赖）。"""

    _NAME_CACHE_TTL = 5.0     # pid -> 进程名缓存有效期（多窗口进程避免反复调 psutil）
    _NAME_CACHE_MAX = 512     # 缓存条目上限，防无限增长

    def __init__(self):
        self.available = HAS_PYWIN32 and HAS_PSUTIL
        self.error = None
        if not HAS_PYWIN32:
            self.error = "未安装 pywin32（pip install pywin32）"
        elif not HAS_PSUTIL:
            self.error = "未安装 psutil（pip install psutil）"
        self._own_pid = os.getpid()
        self._pid_name = {}   # pid -> (进程名小写或 None, 到期 monotonic 时间戳)

    # ---- 进程名解析（带 TTL 缓存） ---------------------------------------
    def _proc_name(self, pid):
        now = time.monotonic()
        hit = self._pid_name.get(pid)
        if hit is not None and hit[1] > now:
            return hit[0]
        name = None
        try:
            name = (psutil.Process(pid).name() or "").lower()
        except Exception:
            name = None
        if len(self._pid_name) >= self._NAME_CACHE_MAX:
            self._pid_name.clear()  # 超限整表清空（简单且够用）
        self._pid_name[pid] = (name, now + self._NAME_CACHE_TTL)
        return name

    # ---- 枚举 ------------------------------------------------------------
    def _collect(self, names):
        """枚举目标程序的可见顶层窗口，返回 [(hwnd, 是否已最小化), ...]；不可用返回 None。"""
        if not self.available:
            return None
        want = {normalize_exe(n).lower() for n in names}
        if not want:
            return []
        found = []

        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return True
            if pid == self._own_pid:
                return True  # 绝不最小化本程序自己的窗口
            if self._proc_name(pid) not in want:
                return True
            iconic = False
            try:
                iconic = win32gui.IsIconic(hwnd)
            except Exception:
                pass
            found.append((hwnd, iconic))
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
        return found

    def find_minimizable(self, names):
        """返回目标程序里"当前未最小化"的可见顶层窗口句柄列表。"""
        found = self._collect(names)
        if not found:
            return []
        return [hwnd for hwnd, iconic in found if not iconic]

    # ---- 最小化 ----------------------------------------------------------
    def minimize(self, names):
        """把目标程序未最小化的顶层窗口全部最小化（已最小化的跳过）。

        Returns:
            {"total":     命中目标的窗口总数,
             "minimized": 本次真正执行最小化的窗口数,
             "skipped":   原本已处于最小化状态的窗口数,
             "failed":    最小化操作失败的窗口数}
        """
        found = self._collect(names)
        if not found:
            return {"total": 0, "minimized": 0, "skipped": 0, "failed": 0}
        minimized = skipped = failed = 0
        for hwnd, iconic in found:
            if iconic:
                skipped += 1
                continue
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                minimized += 1
            except Exception:
                failed += 1  # 个别窗口操作失败不中断其余窗口
        return {"total": len(found), "minimized": minimized,
                "skipped": skipped, "failed": failed}