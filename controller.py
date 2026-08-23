# -*- coding: utf-8 -*-
"""总控制器 (ApplicationController) — 业务规则枢纽。

所有业务判断（if-else）集中于此；GUI 只做显示与交互，
每个用户操作在 GUI 层只调用控制器的一个方法（一行代码）。

依赖方向：controller → core/*（强依赖）；controller → gui/*（仅类型声明，不实例化）。
"""

import time

from PySide6.QtCore import QObject, QTimer, Signal

from core.config import ConfigManager, CONFIG_PATH
from core.engine import PomodoroEngine
from core.guards import GuardianService
from core.presence import PresenceDetector
from core.stats import FocusStats
from core.utils import PHASE_NAMES, normalize_exe


# 摄像头在场检测：休息阶段人离开超过该秒数则关闭遮罩（固定值，不提供配置）
REST_AWAY_SECONDS = 60.0


class ApplicationController(QObject):
    """总控制器：持有所有核心对象，集中处理业务规则。

    信号（输出给 GUI）：
    - tick_updated：200ms 心跳，GUI 用于刷新倒计时显示、状态栏、统计面板
    - phase_changed(str)：阶段切换，GUI 播放提示音
    - log_appended(str)：日志追加
    - overlay_requested(bool, str)：请求显示/隐藏休息遮罩（显示=True + 类型；隐藏=False + 空字符串）
    - stats_changed：统计数据变化，GUI 刷新统计面板
    - presence_alert(str)：摄像头不可用降级提示
    """

    # ---- 输出信号（通知 GUI 更新） ----
    tick_updated = Signal()                    # 200ms 心跳（倒计时/状态栏/统计刷新）
    phase_changed = Signal(str)                # 阶段切换（GUI 据此播放提示音）
    log_appended = Signal(str)                 # 日志追加
    overlay_requested = Signal(bool, str)      # (是否显示, 类型 "short"/"long")
    stats_changed = Signal()                   # 统计页需刷新
    presence_alert = Signal(str)               # 摄像头不可用降级提示

    def __init__(self, config: ConfigManager, engine: PomodoroEngine,
                 presence: PresenceDetector, guards: GuardianService,
                 stats: FocusStats):
        super().__init__()
        self._config = config
        self._engine = engine
        self._presence = presence
        self._guards = guards
        self._stats = stats

        # ---- 业务状态 ----
        self._presence_paused = False       # 是否因"摄像头离开"自动暂停（可被自动恢复）
        self._overlay_visible = False       # 休息遮罩是否显示
        self._overlay_type = ""             # 当前遮罩类型 "short" / "long"
        self._presence_err_logged = False   # 摄像头不可用是否已提示（避免刷屏）
        self._presence_running = False      # 摄像头检测当前是否处于"应运行"状态（识别启用边沿）

        # 连接监管服务日志信号 → 转发给 GUI
        self._guards.log_emitted.connect(self.log_appended)

        # 从配置恢复引擎时长
        self._restore_engine_settings()

        # 启动时初始调度一次：若配置中"全程关闭"为启用状态，开机即生效
        # （与旧版行为一致——全程关闭不受引擎状态限制，启用即运行）
        self._sync_guards_for_state()

        # 主定时器：200ms 驱动引擎检查 + 摄像头规则 + 心跳
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start(200)

    # =====================================================================
    #  公开方法：GUI 每个操作只调用一个
    # =====================================================================

    # ---- 计时控制 ---------------------------------------------------------

    def start_timer(self):
        """开始计时。"""
        self._engine.start()
        self._presence_paused = False
        self.log_appended.emit(f"开始 {PHASE_NAMES[self._engine.phase]}")
        self._sync_guards_for_state()

    def request_pause(self):
        """手动暂停。标记 _presence_paused=False 使摄像头不会自动恢复。"""
        self._engine.pause()
        self._presence_paused = False
        self.log_appended.emit("已暂停")
        self._sync_guards_for_state()

    def resume_timer(self):
        """从暂停恢复。"""
        self._engine.resume()
        self._presence_paused = False  # 手动恢复：接管控制权（人不在会再次自动暂停）
        self.log_appended.emit("继续计时")
        self._sync_guards_for_state()

    def skip_stage(self):
        """跳过当前阶段。"""
        eng = self._engine
        if eng.state == "idle":
            self.start_timer()
            return
        if eng.phase == "work":
            self._log_work_segment(done=False)  # 跳过：已运行时间照常计入
        eng.skip()
        self._presence_paused = False
        self.log_appended.emit(f"⏰ 进入 {PHASE_NAMES[eng.phase]}")
        self._on_phase_transition(eng.phase, check_autostart=False)

    def reset_timer(self):
        """重置计时器。

        重置后按状态重新调度扫描器：工作监管/工作最小化因引擎回到 idle 而停止；
        全程关闭若处于启用状态则继续运行（与旧版"启用即运行"行为一致）。
        """
        self._engine.reset()
        self._presence_paused = False
        self.log_appended.emit("已重置计时器")
        self._hide_overlay()
        self._sync_guards_for_state()

    def apply_timer_settings(self, work_min, short_min, long_min, cycles):
        """应用番茄钟时长设置（更新引擎 + 写入配置）。"""
        self._engine.set_durations(work_min, short_min, long_min, cycles)
        self._config.set("timer", {
            "work_min": work_min,
            "short_min": short_min,
            "long_min": long_min,
            "cycles": cycles,
        })
        self.log_appended.emit(
            f"已更新番茄钟设置: 工作{work_min}分 / 短休{short_min}分 / "
            f"长休{long_min}分 / 每{cycles}个番茄长休")

    # ---- 监管控制 ---------------------------------------------------------

    def toggle_guard(self, enabled: bool):
        """启用/禁用应用监管。"""
        self._config.set("monitor_enabled", enabled)
        self.log_appended.emit("监管已启用" if enabled else "监管已关闭")
        self._sync_guards_for_state()

    def toggle_always_close(self, enabled: bool):
        """启用/禁用全程应用自动关闭。"""
        self._config.set("always_close_enabled", enabled)
        self.log_appended.emit(
            "全程应用自动关闭已启用" if enabled else "全程应用自动关闭已关闭")
        self._sync_guards_for_state()

    def toggle_minimize(self, enabled: bool):
        """启用/禁用工作时段最小化。"""
        self._config.set("minimize_enabled", enabled)
        self.log_appended.emit(
            "工作时段最小化已启用" if enabled else "工作时段最小化已关闭")
        self._sync_guards_for_state()

    # ---- 列表管理 ---------------------------------------------------------

    def add_monitored_app(self, name: str) -> bool:
        """添加监管应用。返回 True=成功添加，False=已存在或名称无效。"""
        exe = normalize_exe(name)
        if not exe:
            self.log_appended.emit("无效的程序名，无法添加")
            return False
        lst = list(self._config.get("monitored", []))
        if exe in lst:
            self.log_appended.emit(f"{exe} 已在监管列表中")
            return False
        lst.append(exe)
        self._config.set("monitored", lst)
        self.log_appended.emit(f"已加入监管: {exe}")
        return True

    def remove_monitored_apps(self, exes):
        """删除多个监管应用。"""
        lst = list(self._config.get("monitored", []))
        for exe in exes:
            if exe in lst:
                lst.remove(exe)
        self._config.set("monitored", lst)
        self.log_appended.emit("已删除选中的监管项")

    def clear_monitored_apps(self):
        """清空监管列表。"""
        self._config.set("monitored", [])
        self.log_appended.emit("已清空监管列表")

    def add_always_app(self, name: str) -> bool:
        """添加全程关闭应用。返回 True=成功添加，False=已存在或名称无效。"""
        exe = normalize_exe(name)
        if not exe:
            self.log_appended.emit("无效的程序名，无法添加")
            return False
        lst = list(self._config.get("always_closed", []))
        if exe in lst:
            self.log_appended.emit(f"{exe} 已在全程关闭列表中")
            return False
        lst.append(exe)
        self._config.set("always_closed", lst)
        self.log_appended.emit(f"已加入全程关闭: {exe}")
        return True

    def remove_always_apps(self, exes):
        lst = list(self._config.get("always_closed", []))
        for exe in exes:
            if exe in lst:
                lst.remove(exe)
        self._config.set("always_closed", lst)
        self.log_appended.emit("已删除选中的全程关闭项")

    def clear_always_apps(self):
        self._config.set("always_closed", [])
        self.log_appended.emit("已清空全程关闭列表")

    def add_minimize_app(self, name: str) -> bool:
        """添加工作最小化应用。返回 True=成功添加，False=已存在或名称无效。"""
        exe = normalize_exe(name)
        if not exe:
            self.log_appended.emit("无效的程序名，无法添加")
            return False
        lst = list(self._config.get("work_minimized", []))
        if exe in lst:
            self.log_appended.emit(f"{exe} 已在工作最小化列表中")
            return False
        lst.append(exe)
        self._config.set("work_minimized", lst)
        self.log_appended.emit(f"已加入工作最小化: {exe}")
        return True

    def remove_minimize_apps(self, exes):
        lst = list(self._config.get("work_minimized", []))
        for exe in exes:
            if exe in lst:
                lst.remove(exe)
        self._config.set("work_minimized", lst)
        self.log_appended.emit("已删除选中的工作最小化项")

    def clear_minimize_apps(self):
        self._config.set("work_minimized", [])
        self.log_appended.emit("已清空工作最小化列表")

    # ---- 设置修改 ---------------------------------------------------------

    def update_setting(self, key: str, value):
        """通用设置更新（即时写盘）。"""
        self._config.set(key, value)

    # ---- 摄像头 -----------------------------------------------------------

    def toggle_presence(self, enabled: bool):
        """启用/禁用摄像头在场检测。"""
        self._config.set("presence_enabled", enabled)
        if not enabled:
            self._presence_paused = False
            self._presence.disable()

    def toggle_rest_overlay(self, enabled: bool):
        """启用/禁用休息全屏遮罩。"""
        self._config.set("rest_overlay", enabled)
        if enabled:
            eng = self._engine
            if eng.state != "idle" and eng.phase != "work":
                self._show_overlay(eng.phase)
        else:
            self._hide_overlay()

    # ---- 统计 -------------------------------------------------------------

    def clear_stats(self):
        """清空统计记录。"""
        self._stats.clear()
        self.stats_changed.emit()

    # ---- 查询方法（供 GUI 读取状态） ----------------------------------------

    @property
    def engine(self) -> PomodoroEngine:
        return self._engine

    @property
    def config(self) -> ConfigManager:
        return self._config

    @property
    def stats(self) -> FocusStats:
        return self._stats

    @property
    def presence(self) -> PresenceDetector:
        return self._presence

    @property
    def guards(self) -> GuardianService:
        return self._guards

    def is_presence_paused(self) -> bool:
        """是否因摄像头离开自动暂停（区分手动暂停）。"""
        return self._presence_paused

    # ---- 引擎状态查询（GUI 只读访问入口，避免直接触碰核心对象） ----

    def engine_state(self) -> str:
        """引擎状态：idle / running / paused。"""
        return self._engine.state

    def engine_phase(self) -> str:
        """当前阶段：work / short_break / long_break。"""
        return self._engine.phase

    def engine_work_count(self) -> int:
        """本周期已完成的番茄数。"""
        return self._engine.work_count

    def engine_cycles(self) -> int:
        """每几个番茄进入一次长休息。"""
        return self._engine.cycles_before_long

    def remaining_seconds(self) -> float:
        """当前阶段剩余秒数（含暂停时保留值）。"""
        return self._engine.remaining_seconds()

    def is_overlay_visible(self) -> bool:
        return self._overlay_visible

    def enforcement_active(self) -> bool:
        """当前是否应当执行应用监管（仅工作阶段 + 运行中生效）。"""
        if not self._config.get("monitor_enabled", True):
            return False
        eng = self._engine
        return eng.state == "running" and eng.phase == "work"

    def always_close_active(self) -> bool:
        """当前是否应当执行全程关闭。"""
        return bool(self._config.get("always_close_enabled", False))

    def minimize_active(self) -> bool:
        """当前是否应当执行工作时段最小化。"""
        if not self._config.get("minimize_enabled", False):
            return False
        eng = self._engine
        return eng.state == "running" and eng.phase == "work"

    def monitor_status_text(self) -> str:
        """监管状态文案（状态栏 / 托盘菜单）。"""
        if not self._config.get("monitor_enabled", True):
            return "监管关闭"
        if self.enforcement_active():
            return "监管中"
        return "监管暂停(休息/未开始)"

    def presence_status(self) -> str:
        """摄像头状态文案（状态栏）。"""
        if not self._config.get("presence_enabled", True):
            return "摄像头: 关"
        det = self._presence
        if det is None or not det.is_available():
            return "摄像头: 不可用"
        if not self._presence_should_run():
            return "摄像头: 待命"
        return "摄像头: 人在" if det.is_present() else "摄像头: 人不在"

    def live_focus_extra(self) -> int:
        """当前工作阶段已运行（未落库）的秒数，用于统计页今日档实时叠加。"""
        eng = self._engine
        if eng.phase == "work" and eng.state in ("running", "paused"):
            return int(eng.work_sec - eng.remaining_seconds())
        return 0

    def config_path(self) -> str:
        return CONFIG_PATH

    def process_backend_name(self) -> str:
        """进程检测后端显示名（启动日志用）。"""
        backend = self._guards._work._pm.backend
        return {"psutil": "psutil", "powershell": "PowerShell",
                "tasklist": "tasklist/taskkill (内置)"}.get(backend, backend)

    def minimizer_status(self):
        """工作时段最小化功能可用性 → (available: bool, error: str|None)。"""
        mgr = self._guards._minimize._minimizer
        return mgr.available, mgr.error

    # ---- 程序退出 ---------------------------------------------------------

    def on_quit(self):
        """程序退出前的清理：落库 + 释放摄像头 + 停止扫描器。"""
        self._tick_timer.stop()  # 先停心跳：防止后续 tick 访问已关闭的统计库
        self._hide_overlay()
        self._presence.stop()
        self._log_work_segment(done=False)
        self._stats.close()
        self._guards.stop_all()

    # =====================================================================
    #  内部业务规则（核心逻辑聚集地）
    # =====================================================================

    def _on_tick(self):
        """200ms 定时驱动：检测阶段自然结束 + 摄像头规则 + 发射心跳。"""
        eng = self._engine

        # 1. 检测阶段自然结束（倒计时归零 → 自动推进）
        if eng.state == "running" and eng.remaining_seconds() <= 0:
            if eng.phase == "work":
                self._log_work_segment(done=True)  # 自然结束的番茄
            new_phase, finished_work = eng.advance()
            msg = f"⏰ {PHASE_NAMES[new_phase]} 开始"
            if finished_work:
                msg += f"（已完成{eng.work_count}个番茄）"
            self.log_appended.emit(msg)
            self._on_phase_transition(new_phase, check_autostart=True)

        # 2. 摄像头在场检测：按状态调度启停
        self._update_presence()

        # 3. 应用摄像头规则（离开暂停 / 回来恢复 / 休息无人关遮罩）
        self._check_presence_rules()

        # 4. 发射心跳（GUI 用此信号刷新倒计时、状态栏、统计面板）
        self.tick_updated.emit()

    def _on_phase_transition(self, new_phase, check_autostart=False):
        """阶段切换时的统一处理：遮罩 + 强制清场 + 监管调度 + 自动暂停判定。

        提示音由 GUI 在收到 phase_changed 信号后自行播放（音效是 GUI 关注点）。
        """
        eng = self._engine

        # 通知 GUI 播放提示音
        self.phase_changed.emit(new_phase)

        # 休息开始时强制关闭监管应用（如果配置了）
        if (new_phase != "work"
                and self._config.get("force_on_break", False)
                and self._config.get("monitored", [])):
            self.log_appended.emit("—— 进入休息，开始强制关闭监管应用 ——")
            self._guards.force_scan("work")

        # 同步遮罩（休息显示 / 工作隐藏）
        self._sync_overlay(new_phase)

        # 新阶段重置离开判定
        self._presence_paused = False

        # 如果不自动开始下一阶段，暂停引擎
        if check_autostart and not self._config.get("autostart", True):
            eng.pause()

        # 根据新阶段调整监管扫描器
        self._sync_guards_for_state()

    def _restore_engine_settings(self):
        """从配置恢复引擎时长。"""
        timer = self._config.get("timer", {})
        try:
            self._engine.set_durations(
                timer.get("work_min", 25),
                timer.get("short_min", 5),
                timer.get("long_min", 15),
                timer.get("cycles", 4),
            )
        except (ValueError, TypeError):
            pass

    # ---- 统计 ----

    def _log_work_segment(self, done):
        """记录当前工作阶段已运行时长到统计库。"""
        eng = self._engine
        if eng.phase != "work":
            return
        elapsed = eng.work_sec - eng.remaining_seconds()
        self._stats.add_work_segment(elapsed, done=done)
        self.stats_changed.emit()

    # ---- 遮罩控制 ----

    def _sync_overlay(self, phase):
        """按阶段同步遮罩：休息显示，工作隐藏。"""
        if phase == "work":
            self._hide_overlay()
        else:
            self._show_overlay(phase)

    def _show_overlay(self, phase="short_break"):
        """请求显示休息遮罩（发射信号给 GUI）。"""
        if not self._config.get("rest_overlay", True):
            return
        # 宽限期从"休息开始"起算：重置最后看到人的时刻，避免工作段遗留的
        # 离开时长让遮罩刚显示就被判定"休息无人超时"而立即关闭
        self._presence.reset_last_seen()
        overlay_type = "long" if phase == "long_break" else "short"
        self._overlay_visible = True
        self._overlay_type = overlay_type
        self.overlay_requested.emit(True, overlay_type)
        if overlay_type == "long":
            self.log_appended.emit("—— 长休息遮罩已显示，请喝水并看向6米外物体 ——")
        else:
            self.log_appended.emit("—— 短休息遮罩已显示，请看向6米外物体并眨眼 ——")

    def _hide_overlay(self):
        """请求隐藏休息遮罩（发射信号给 GUI）。"""
        if self._overlay_visible:
            self._overlay_visible = False
            self.overlay_requested.emit(False, "")
            self.log_appended.emit("休息遮罩已关闭")

    # ---- 摄像头在场检测 ----

    def _presence_should_run(self) -> bool:
        """当前状态是否需要启用摄像头检测。"""
        if not self._config.get("presence_enabled", True):
            return False
        eng = self._engine
        if self._overlay_visible:
            return True  # 休息遮罩显示中：人在保持遮罩，离开超时关闭
        if eng.phase == "work" and (eng.state == "running" or self._presence_paused):
            return True  # 工作计时中 或 "离开暂停"等待自动恢复
        return False

    def _update_presence(self):
        """按当前状态启停摄像头检测（幂等，每 tick 调用）。"""
        should = (self._config.get("presence_enabled", True)
                  and self._presence_should_run())
        if not should:
            self._presence.disable()
            self._presence_running = False
            return
        if not self._presence_running:
            # 启用边沿：本次启用允许一条降级提示（防止不可用时每 tick 刷屏）
            self._presence_err_logged = False
            self._presence_running = True
        self._presence.enable()  # 幂等；内部线程失败退出后由下次调用重试

    def _check_presence_rules(self):
        """应用摄像头规则：专注离开暂停 / 回来自动恢复 / 休息无人关遮罩。"""
        det = self._presence
        if det is None:
            return
        if not det.is_available():
            # 降级提示（只记录一次，避免刷屏）
            err = det.error()
            if err and not self._presence_err_logged:
                self._presence_err_logged = True
                self.presence_alert.emit(f"摄像头检测不可用，已降级: {err}")
            return
        self._presence_err_logged = False

        eng = self._engine

        if eng.phase == "work" and eng.state == "running":
            # 专注：人不在超过宽限期 → 自动暂停
            grace = self._config.get("presence_grace_sec", 15.0)
            if not det.is_present() and det.last_seen_age() > grace:
                self._presence_paused = True
                eng.pause()
                self.log_appended.emit("摄像头未检测到人，自动暂停计时")
                self._sync_guards_for_state()

        elif (eng.phase == "work" and eng.state == "paused"
              and self._presence_paused):
            # 离开暂停：人回到摄像头前 → 自动恢复
            if det.is_present():
                self._presence_paused = False
                eng.resume()
                self.log_appended.emit("检测到人回到摄像头前，自动继续计时")
                self._sync_guards_for_state()

        elif eng.phase != "work" and self._overlay_visible:
            # 休息：人不在超过 1 分钟 → 关闭遮罩（本次休息不再重新打开）
            if not det.is_present() and det.last_seen_age() > REST_AWAY_SECONDS:
                self.log_appended.emit("超过1分钟未检测到人，关闭休息遮罩")
                self._hide_overlay()

    # ---- 监管扫描器调度 ----

    def _sync_guards_for_state(self):
        """根据当前引擎状态与配置，启动 / 停止对应的扫描器。"""
        eng = self._engine

        # 工作监管：仅工作 + 运行中
        if (eng.state == "running" and eng.phase == "work"
                and self._config.get("monitor_enabled", True)):
            self._guards.start("work")
        else:
            self._guards.stop("work")

        # 全程关闭：启用即运行（不受引擎状态限制，与原行为一致）
        if self._config.get("always_close_enabled", False):
            self._guards.start("always")
        else:
            self._guards.stop("always")

        # 工作最小化：仅工作 + 运行中
        if (eng.state == "running" and eng.phase == "work"
                and self._config.get("minimize_enabled", False)):
            self._guards.start("minimize")
        else:
            self._guards.stop("minimize")
