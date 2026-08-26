# -*- coding: utf-8 -*-
"""番茄钟状态机 (PomodoroEngine)。"""

import time


class PomodoroEngine:
    """番茄钟状态机。

    阶段: work -> short_break/long_break -> work -> ...
    每完成 cycles_before_long 个工作阶段后进入一次长休息。

    特殊阶段 restart（检查点 SOS 重启流程）：
      restart = 遮罩期(restart_break_sec=60s) -> 工作期(restart_work_sec=300s)
      restart_stage 区分当前子阶段；遮罩期结束进入工作期，工作期结束进入短休息。
      重启工作期计入专注统计（由 controller 落库），不计周期计数/streak。
    """

    # 重启阶段：遮罩期与工作期时长（固定）
    restart_break_sec = 60.0
    restart_work_sec = 300.0

    def __init__(self, work_min=25, short_min=5, long_min=15,
                 cycles_before_long=4):
        self.set_durations(work_min, short_min, long_min, cycles_before_long)
        self.phase = "work"
        self.state = "idle"          # idle | running | paused
        self.work_count = 0          # 本周期已完成的番茄数
        self.phase_end = 0.0         # monotonic 时间戳
        self.remaining = 0.0         # 暂停时保留的剩余秒数
        self.restart_stage = "break"  # restart 子阶段: break(遮罩) | work(工作)

    def set_durations(self, work_min, short_min, long_min, cycles_before_long):
        self.work_sec = max(0.1, float(work_min) * 60)
        self.short_sec = max(0.1, float(short_min) * 60)
        self.long_sec = max(0.1, float(long_min) * 60)
        self.cycles_before_long = max(1, int(cycles_before_long))

    def phase_duration(self, phase=None):
        phase = phase or self.phase
        if phase == "restart":
            return (self.restart_break_sec if self.restart_stage == "break"
                    else self.restart_work_sec)
        return {"work": self.work_sec, "short_break": self.short_sec,
                "long_break": self.long_sec}[phase]

    # ---- 控制 -----------------------------------------------------------
    def start(self):
        if self.state == "idle":
            self.phase = "work"
            self.work_count = 0
        self.state = "running"
        self.phase_end = time.monotonic() + self.phase_duration()
        self.remaining = 0.0

    def pause(self):
        if self.state == "running":
            self.remaining = self.remaining_seconds()
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self.state = "running"
            self.phase_end = time.monotonic() + max(0.1, self.remaining)
            self.remaining = 0.0

    def reset(self):
        self.state = "idle"
        self.phase = "work"
        self.work_count = 0
        self.remaining = self.phase_duration()

    def skip(self):
        """跳过当前阶段，直接进入下一阶段。"""
        self.advance()

    # ---- 重启流程（检查点 SOS 专用） ----------------------------------

    def enter_restart(self):
        """进入重启阶段（遮罩期开始）：1 分钟遮罩 → 5 分钟工作 → 短休息。"""
        self.phase = "restart"
        self.restart_stage = "break"
        self.state = "running"
        self.phase_end = time.monotonic() + self.restart_break_sec
        self.remaining = 0.0

    def restart_break_done(self):
        """重启遮罩期结束 → 工作期（5 分钟固定工作）。"""
        self.restart_stage = "work"
        self.state = "running"
        self.phase_end = time.monotonic() + self.restart_work_sec
        self.remaining = 0.0

    def restart_work_done(self):
        """重启工作期结束 → 正常短休息（周期计数已归零，从第一个番茄重新开始）。"""
        self.phase = "short_break"
        self.restart_stage = "break"  # 复位，供下次重启流程使用
        self.state = "running"
        self.phase_end = time.monotonic() + self.short_sec
        self.remaining = 0.0

    def remaining_seconds(self):
        if self.state == "paused":
            return max(0.0, self.remaining)
        if self.state == "running":
            return max(0.0, self.phase_end - time.monotonic())
        return self.phase_duration()

    def advance(self):
        """推进到下一阶段。返回 (新阶段, 是否结束了一个工作番茄)。

        restart 阶段按子阶段推进：遮罩期 → 工作期 → 短休息。
        """
        if self.phase == "restart":
            if self.restart_stage == "break":
                self.restart_break_done()
                return self.phase, False
            self.restart_work_done()
            return self.phase, False
        finished_work = False
        if self.phase == "work":
            self.work_count += 1
            finished_work = True
            if self.work_count % self.cycles_before_long == 0:
                self.phase = "long_break"
            else:
                self.phase = "short_break"
        else:
            self.phase = "work"
        self.state = "running"
        self.phase_end = time.monotonic() + self.phase_duration()
        self.remaining = 0.0
        return self.phase, finished_work