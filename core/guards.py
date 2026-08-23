# -*- coding: utf-8 -*-
"""监管服务 (GuardianService)。

包含三个独立的 QThread 后台扫描器：
1. WorkGuard：应用监管，仅在工作阶段由控制器启动。
2. AlwaysGuard：全程应用自动关闭，从番茄钟启动到结束全程运行。
3. MinimizeGuard：工作时段最小化应用，仅在工作阶段由控制器启动。

所有扫描器均运行在独立的 QThread 中，通过 log_emitted 信号输出日志。
"""

import time

from PySide6.QtCore import (QMetaObject, QObject, QThread, QTimer, Qt, Signal,
                            Slot)

from .process_manager import ProcessManager
from .window_minimize import WindowMinimizeManager


class _BaseGuard(QObject):
    """扫描器基类：运行在独立 QThread 中，通过 QTimer 周期触发扫描。"""

    log_emitted = Signal(str)

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._scan)

    @Slot()
    def start(self):
        """启动定时扫描；首次立即执行一次。

        幂等：定时器已在运行则直接返回，避免控制器高频调度时重复排队
        立即扫描（间隔变化由 _scan 内部的实时重设定时器逻辑处理）。
        """
        if self._timer.isActive():
            return
        self._timer.start(int(self._interval() * 1000))
        # 立即执行一次，保证控制器启动后立刻生效
        QTimer.singleShot(0, self._scan)

    @Slot()
    def stop(self):
        """停止定时扫描。"""
        self._timer.stop()

    @Slot()
    def force_scan(self):
        """立即执行一次扫描（用于进入休息时强制清场）。

        强制扫描绕过启用开关和失败冷却，与旧版 force=True 行为一致。
        """
        self._scan(force=True)

    def _interval(self):
        return self._config.get("interval", 2.0)

    def _scan(self, force=False):
        """周期扫描入口：QTimer.timeout 连接此处（默认 force=False）。

        执行子类 _do_scan 后，再检查扫描间隔是否被用户修改——
        若已变化则立即重设定时器，使新间隔实时生效（无需重启扫描器）。
        """
        self._do_scan(force)
        # 扫描间隔实时跟随配置（变更后立即生效，无需重启扫描器）
        try:
            want_ms = int(max(0.1, float(self._interval())) * 1000)
        except (TypeError, ValueError):
            return
        if self._timer.isActive() and self._timer.interval() != want_ms:
            self._timer.start(want_ms)

    def _do_scan(self, force):
        raise NotImplementedError


class WorkGuard(_BaseGuard):
    """应用监管：检测并强制关闭监管列表中的程序。"""

    def __init__(self, config):
        super().__init__(config)
        self._pm = ProcessManager()
        self._fail_cooldown = {}
        self._fail_msg = {}

    def _do_scan(self, force=False):
        # 强制扫描（休息前清场）绕过启用开关和失败冷却，与旧版 force=True 一致
        if not force and not self._config.get("monitor_enabled", True):
            return
        names = self._config.get("monitored", [])
        if not names:
            return
        running = self._pm.find_running(names)
        now = time.monotonic()
        for exe in running:
            if not force and now < self._fail_cooldown.get(exe, 0):
                continue
            ok, detail = self._pm.kill(exe)
            if ok:
                self._fail_cooldown.pop(exe, None)
                self._fail_msg.pop(exe, None)
                self.log_emitted.emit(
                    f"✂ {time.strftime('%H:%M:%S')} [监管] 已强制关闭 {exe}（{detail}）")
            else:
                if detail == self._fail_msg.get(exe):
                    self._fail_cooldown[exe] = time.monotonic() + 60
                    self.log_emitted.emit(
                        f"! [监管] 关闭 {exe} 失败: {detail}；同一错误 60 秒内不再重试")
                else:
                    self._fail_msg[exe] = detail
                    msg = f"! [监管] 关闭 {exe} 失败: {detail}"
                    if "权限" in detail or "denied" in detail.lower() or "拒绝" in detail:
                        msg += "；目标可能以管理员权限运行或受反作弊保护，建议以管理员身份运行本程序"
                    self.log_emitted.emit(msg)


class AlwaysGuard(_BaseGuard):
    """全程应用自动关闭：不受阶段限制，启动后全程强制关闭。"""

    def __init__(self, config):
        super().__init__(config)
        self._pm = ProcessManager()
        self._fail_cooldown = {}
        self._fail_msg = {}

    def _do_scan(self, force=False):
        # 强制扫描绕过启用开关和失败冷却
        if not force and not self._config.get("always_close_enabled", False):
            return
        names = self._config.get("always_closed", [])
        if not names:
            return
        running = self._pm.find_running(names)
        now = time.monotonic()
        for exe in running:
            if not force and now < self._fail_cooldown.get(exe, 0):
                continue
            ok, detail = self._pm.kill(exe)
            if ok:
                self._fail_cooldown.pop(exe, None)
                self._fail_msg.pop(exe, None)
                self.log_emitted.emit(
                    f"✂ {time.strftime('%H:%M:%S')} [全程关闭] 已强制关闭 {exe}（{detail}）")
            else:
                if detail == self._fail_msg.get(exe):
                    self._fail_cooldown[exe] = time.monotonic() + 60
                    self.log_emitted.emit(
                        f"! [全程关闭] 关闭 {exe} 失败: {detail}；同一错误 60 秒内不再重试")
                else:
                    self._fail_msg[exe] = detail
                    msg = f"! [全程关闭] 关闭 {exe} 失败: {detail}"
                    if "权限" in detail or "denied" in detail.lower() or "拒绝" in detail:
                        msg += "；目标可能以管理员权限运行或受反作弊保护，建议以管理员身份运行本程序"
                    self.log_emitted.emit(msg)


class MinimizeGuard(_BaseGuard):
    """工作时段最小化应用。"""

    def __init__(self, config):
        super().__init__(config)
        self._minimizer = WindowMinimizeManager()

    def _interval(self):
        return 5.0

    def _do_scan(self, force=False):
        # 强制扫描绕过启用开关
        if not self._minimizer.available:
            return
        if not force and not self._config.get("minimize_enabled", False):
            return
        names = self._config.get("work_minimized", [])
        if not names:
            return
        try:
            result = self._minimizer.minimize(names)
        except Exception as exc:
            self.log_emitted.emit(f"! [工作最小化] 扫描失败: {exc}")
            return
        if result["total"]:
            msg = (f"— {time.strftime('%H:%M:%S')} [工作最小化] 共找到 "
                   f"{result['total']} 个窗口，已最小化 {result['minimized']} 个，"
                   f"{result['skipped']} 个原本已处于最小化状态")
            if result["failed"]:
                msg += f"，{result['failed']} 个操作失败"
            self.log_emitted.emit(msg)


class GuardianService(QObject):
    """监管服务总入口：管理三个扫描器的启停与日志转发。"""

    log_emitted = Signal(str)

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._work = WorkGuard(config)
        self._always = AlwaysGuard(config)
        self._minimize = MinimizeGuard(config)

        self._worker_by_scope = {
            "work": self._work,
            "always": self._always,
            "minimize": self._minimize,
        }

        # 每个 worker 运行在独立的 QThread 事件循环中
        self._threads = {}
        for scope, worker in self._worker_by_scope.items():
            worker.log_emitted.connect(self.log_emitted)
            thread = QThread()
            worker.moveToThread(thread)
            thread.start()
            self._threads[scope] = thread

    def start(self, scope):
        """启动指定扫描器（"work" / "always" / "minimize"）。"""
        worker = self._worker_by_scope.get(scope)
        if worker:
            QMetaObject.invokeMethod(worker, "start", Qt.QueuedConnection)

    def stop(self, scope):
        """停止指定扫描器。"""
        worker = self._worker_by_scope.get(scope)
        if worker:
            QMetaObject.invokeMethod(worker, "stop", Qt.QueuedConnection)

    def force_scan(self, scope):
        """立即触发一次指定扫描器的扫描（用于休息前强制清场）。"""
        worker = self._worker_by_scope.get(scope)
        if worker:
            QMetaObject.invokeMethod(worker, "force_scan", Qt.QueuedConnection)

    def stop_all(self):
        """停止所有扫描器并退出线程（程序退出时使用）。

        用 QueuedConnection 让 stop 槽在各自 worker 线程内执行
        （QTimer 归属 worker 线程，跨线程直接调用会触发 Qt 警告）；
        队列事件先进先出，stop 会在 quit 之前被处理。
        扫描可能在执行阻塞式 kill()（最长 15s），等待上限放宽到 5s。
        """
        for worker in self._worker_by_scope.values():
            QMetaObject.invokeMethod(worker, "stop", Qt.QueuedConnection)
        for thread in self._threads.values():
            thread.quit()
            thread.wait(5000)