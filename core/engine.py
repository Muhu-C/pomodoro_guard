# -*- coding: utf-8 -*-
"""番茄钟状态机 (PomodoroEngine)。"""

import time


class PomodoroEngine:
    """番茄钟状态机。

    阶段: work -> short_break/long_break -> work -> ...
    每完成 cycles_before_long 个工作阶段后进入一次长休息。
    """

    def __init__(self, work_min=25, short_min=5, long_min=15,
                 cycles_before_long=4):
        self.set_durations(work_min, short_min, long_min, cycles_before_long)
        self.phase = "work"
        self.state = "idle"          # idle | running | paused
        self.work_count = 0          # 本周期已完成的番茄数
        self.phase_end = 0.0         # monotonic 时间戳
        self.remaining = 0.0         # 暂停时保留的剩余秒数

    def set_durations(self, work_min, short_min, long_min, cycles_before_long):
        self.work_sec = max(0.1, float(work_min) * 60)
        self.short_sec = max(0.1, float(short_min) * 60)
        self.long_sec = max(0.1, float(long_min) * 60)
        self.cycles_before_long = max(1, int(cycles_before_long))

    def phase_duration(self, phase=None):
        phase = phase or self.phase
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

    def remaining_seconds(self):
        if self.state == "paused":
            return max(0.0, self.remaining)
        if self.state == "running":
            return max(0.0, self.phase_end - time.monotonic())
        return self.phase_duration()

    def advance(self):
        """推进到下一阶段。返回 (新阶段, 是否结束了一个工作番茄)。"""
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