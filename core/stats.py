# -*- coding: utf-8 -*-
"""
专注数据统计 —— 核心逻辑层（UI 无关）
======================================
用 SQLite 记录每段"工作运行"时长，支持按日/周/月/累计聚合，
以及"连续专注天数"(streak)。

数据口径：
- 每个工作阶段"运行中"的时间记为一整段（手动暂停/摄像头离开暂停的时间不计入，
  由上层用 remaining 差值计算，本模块只负责落库与查询）；
- done=1 表示该段以自然倒计时结束（跳过/退出等记 0），用于 streak；
- "今日"实时部分（当前工作阶段已运行但未结束的时长）由 UI 层叠加，不写库，
  只有阶段结束/退出时才落库 —— 中途崩溃也不丢已结束的段。
"""

import os
import sqlite3
import sys
import time

DB_NAME = "pomodoro_guard_stats.db"


def _default_db_path():
    """统计库与程序本体同目录（exe 打包后与 exe 同目录，与配置文件一致）。

    本模块位于 core/ 子包内，非冻结态下须向上回溯一层到项目根目录，
    以保持统计库位置与重构前一致（与 main.py / 打包后的 exe 同级）。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                            DB_NAME)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        DB_NAME)


class FocusStats:
    """专注时长统计（SQLite，单线程使用；UI 主线程调用）。"""

    def __init__(self, db_path=None):
        self.db_path = db_path or _default_db_path()
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS focus_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts INTEGER NOT NULL,"          # unix 秒（段结束时刻）
            " duration_sec INTEGER NOT NULL,"  # 该段工作运行时长
            " done INTEGER NOT NULL DEFAULT 0)"  # 1=自然结束的番茄
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ts ON focus_log(ts)")
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------------------------- 写入 ----------------------------
    def add_work_segment(self, duration_sec, done=False):
        """记录一段工作运行时长。done=True 表示该段自然倒计时结束。"""
        duration = max(0, int(duration_sec))
        if duration <= 0:
            return
        self._conn.execute(
            "INSERT INTO focus_log(ts, duration_sec, done) VALUES(?,?,?)",
            (int(time.time()), duration, 1 if done else 0))
        self._conn.commit()

    def clear(self):
        """清空全部统计记录。"""
        self._conn.execute("DELETE FROM focus_log")
        self._conn.commit()

    # ---------------------------- 查询 ----------------------------
    @staticmethod
    def _day_start(day_lt):
        """某本地日期的 0 点 unix 时间戳。"""
        return int(time.mktime((day_lt.tm_year, day_lt.tm_mon, day_lt.tm_mday,
                                0, 0, 0, 0, 0, -1)))

    def _duration_between(self, start_ts, end_ts):
        row = self._conn.execute(
            "SELECT COALESCE(SUM(duration_sec), 0) FROM focus_log"
            " WHERE ts>=? AND ts<?", (start_ts, end_ts)).fetchone()
        return int(row[0])

    def today_duration(self):
        """今日（本地日）累计专注秒数（不含当前未结束段，实时部分由 UI 叠加）。"""
        now = time.localtime(time.time())
        start = self._day_start(now)
        return self._duration_between(start, start + 86400)

    def daily_totals(self, days):
        """近 days 天（含今天）的每日专注秒数，按日期升序：{ 'YYYY-MM-DD': 秒 }。

        无记录日为 0，保证图表坐标轴完整。
        """
        today_lt = time.localtime(time.time())
        day0 = self._day_start(time.localtime(
            time.mktime((today_lt.tm_year, today_lt.tm_mon,
                         today_lt.tm_mday - (days - 1), 0, 0, 0, 0, 0, -1))))
        out = {}
        for i in range(days - 1, -1, -1):
            lt = time.localtime(
                time.mktime((today_lt.tm_year, today_lt.tm_mon,
                             today_lt.tm_mday - i, 0, 0, 0, 0, 0, -1)))
            out[time.strftime("%Y-%m-%d", lt)] = 0
        rows = self._conn.execute(
            "SELECT ts, duration_sec FROM focus_log WHERE ts>=?",
            (day0,)).fetchall()
        for ts, dur in rows:
            key = time.strftime("%Y-%m-%d", time.localtime(ts))
            if key in out:
                out[key] += dur
        return dict(sorted(out.items()))

    def monthly_totals(self, limit=24):
        """按月聚合（有记录的月份，升序，最多 limit 个）：[('YYYY-MM', 秒), ...]"""
        rows = self._conn.execute(
            "SELECT ts, duration_sec FROM focus_log").fetchall()
        out = {}
        for ts, dur in rows:
            key = time.strftime("%Y-%m", time.localtime(ts))
            out[key] = out.get(key, 0) + dur
        return sorted(out.items())[-limit:]

    def streak(self):
        """连续专注天数：从今天往回数连续天数。

        今天必须有自然结束的番茄（done=1）才从今天起算，否则视为
        "今天尚未完成"而返回 0（昨天的连续不会在当天未完成时保留）；
        中断一天后归零。
        """
        today_lt = time.localtime(time.time())
        n = 0
        day = today_lt
        while True:
            start = self._day_start(day)
            row = self._conn.execute(
                "SELECT COUNT(*) FROM focus_log"
                " WHERE ts>=? AND ts<? AND done=1",
                (start, start + 86400)).fetchone()
            if row[0] > 0:
                n += 1
                day = time.localtime(start - 1)
            else:
                break
        return n