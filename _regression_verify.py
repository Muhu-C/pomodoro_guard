# -*- coding: utf-8 -*-
"""核心功能回归验证（原 _step11_verify.py 已被清理，此脚本重建覆盖，
同时确保检查点功能改动未破坏原有行为）。

覆盖：开始/暂停/继续/跳过/重置、三扫描器生命周期、摄像头自动暂停/恢复、
休息遮罩短/长、统计数据、配置持久化、托盘/显隐。
"""
import json
import os
import shutil
import sys
import time
import threading

PROJECT = r"E:\dshn-workspace\pomodoro_guard"
sys.path.insert(0, PROJECT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PASS, _FAIL = [], []


def check(name, cond, extra=""):
    (_PASS if cond else _FAIL).append(name)
    marker = "PASS" if cond else "FAIL"
    print(f"  {marker} {name}" + (f"   [{extra}]" if extra else ""))


def pump(seconds=0.1):
    app = QApplication.instance()
    if app is None:
        return
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_until(pred, timeout=2.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        pump(interval)
    return False


_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_reg_tmp")
os.makedirs(_TMP, exist_ok=True)
_tmp_config = os.path.join(_TMP, "pomodoro_guard_config.json")
_tmp_db = os.path.join(_TMP, "stats.db")
_real_config = os.path.join(PROJECT, "pomodoro_guard_config.json")
if os.path.exists(_real_config):
    shutil.copy2(_real_config, _tmp_config)
else:
    with open(_tmp_config, "w", encoding="utf-8") as f:
        json.dump({}, f)
if os.path.exists(_tmp_db):
    os.remove(_tmp_db)

import core.config as _cfgmod
_cfgmod.CONFIG_PATH = _tmp_config
import core.stats as _statsmod
_statsmod._default_db_path = lambda: _tmp_db


class PresenceStub:
    def __init__(self):
        self._lock = threading.Lock()
        self._available = True
        self._present = True
        self._last_seen_time = time.monotonic()

    def set_present(self, v):
        with self._lock:
            self._present = bool(v)
            if self._present:
                self._last_seen_time = time.monotonic()

    def reset_last_seen(self):
        with self._lock:
            self._last_seen_time = time.monotonic()

    def is_available(self):
        return self._available

    def is_present(self):
        with self._lock:
            return self._present

    def last_seen_age(self):
        with self._lock:
            return max(0.0, time.monotonic() - self._last_seen_time)

    def error(self):
        return ""

    def enable(self):
        pass

    def disable(self):
        with self._lock:
            self._present = False

    def stop(self):
        self.disable()


import core.presence as _presmod
_presmod._PresenceStub = PresenceStub

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

print("=" * 60)
print("  POMODORO GUARD - Core Regression Tests")
print("=" * 60)

app = QApplication(sys.argv)
app.setStyle("Fusion")

import main as m


class AutoQuitWindow(m.MainWindow):
    def show(self):
        super().show()
        QTimer.singleShot(2000, QApplication.instance().quit)


m.MainWindow = AutoQuitWindow


def build_controller(presence_stub=None, monitor=True, always=False,
                     minimize=False):
    from core.config import ConfigManager
    from core.engine import PomodoroEngine
    from core.guards import GuardianService
    from core.stats import FocusStats
    from controller import ApplicationController

    config = ConfigManager()
    # 显式隔离检查点配置（防止共享配置残留干扰）
    config.set("checkpoints_enabled", False)
    config.set("checkpoints", [])
    config.set("monitor_enabled", monitor)
    config.set("always_close_enabled", always)
    config.set("minimize_enabled", minimize)
    engine = PomodoroEngine()
    stats = FocusStats()
    guards = GuardianService(config)
    if presence_stub is None:
        presence_stub = _presmod._PresenceStub()
    ctrl = ApplicationController(config, engine, presence_stub, guards, stats)
    return config, engine, presence_stub, guards, stats, ctrl


def cleanup(ctrl):
    if ctrl:
        ctrl.on_quit()
        pump(0.5)


pump(0.2)


# =================================================================== #
#  1. 开始 / 暂停 / 继续 / 跳过 / 重置                                   #
# =================================================================== #

print("\n-- 1. Timer control --")

config, engine, pres_stub, guards, stats, ctrl = build_controller(
    monitor=True, minimize=True)
win = m.MainWindow(ctrl)
win.show()
pump(0.5)

check("1a Initial idle", engine.state == "idle")
ctrl.start_timer()
pump(0.3)
check("1b Running in work", engine.state == "running" and engine.phase == "work")
r1 = engine.remaining_seconds()
ctrl.request_pause()
pump(0.2)
check("1c Paused", engine.state == "paused")
check("1d Remaining frozen", abs(engine.remaining_seconds() - r1) < 0.6)
ctrl.resume_timer()
pump(0.2)
check("1e Resumed", engine.state == "running")
check("1f Remaining decreasing",
      engine.remaining_seconds() < r1 + 0.1)
ctrl.skip_stage()
pump(0.3)
check("1g Skip -> short_break", engine.phase == "short_break")
ctrl.skip_stage()
pump(0.3)
check("1h Skip -> work", engine.phase == "work")
ctrl.reset_timer()
pump(0.2)
check("1i Reset -> idle", engine.state == "idle" and engine.phase == "work")
check("1j Work count reset", engine.work_count == 0)
cleanup(ctrl)


# =================================================================== #
#  2. 三扫描器生命周期（工作/休息/暂停）                                  #
# =================================================================== #

print("\n-- 2. Guard lifecycle --")

config, engine, pres_stub, guards, stats, ctrl = build_controller(
    monitor=True, always=True, minimize=True)
win2 = m.MainWindow(ctrl)
win2.show()
pump(0.5)

ctrl.start_timer()
check("2a Work: work-guard active",
      wait_until(lambda: ctrl._guards._work._timer.isActive()))
check("2b Work: always active", ctrl._guards._always._timer.isActive())
check("2c Work: minimize active",
      wait_until(lambda: ctrl._guards._minimize._timer.isActive()))

ctrl.skip_stage()
check("2d Rest: work-guard stopped",
      wait_until(lambda: not ctrl._guards._work._timer.isActive()))
check("2e Rest: always still active", ctrl._guards._always._timer.isActive())
check("2f Rest: minimize stopped",
      wait_until(lambda: not ctrl._guards._minimize._timer.isActive()))

ctrl.reset_timer()
ctrl.start_timer()
pump(0.3)
ctrl.request_pause()
check("2g Pause: work-guard stopped",
      wait_until(lambda: not ctrl._guards._work._timer.isActive()))
check("2h Pause: always still active", ctrl._guards._always._timer.isActive())
check("2i Pause: minimize stopped",
      wait_until(lambda: not ctrl._guards._minimize._timer.isActive()))
cleanup(ctrl)


# =================================================================== #
#  3. 摄像头离位自动暂停 / 回来自动恢复                                   #
# =================================================================== #

print("\n-- 3. Camera presence auto-pause/resume --")

config, engine, pres_stub, guards, stats, ctrl = build_controller()
ctrl.toggle_presence(True)
ctrl.update_setting("presence_enabled", True)
ctrl.update_setting("presence_grace_sec", 1.0)
win3 = m.MainWindow(ctrl)
win3.show()
pump(0.5)

pres_stub.set_present(True)
ctrl.start_timer()
pump(0.3)
check("3a Running with human present", engine.state == "running")

pres_stub.set_present(False)
check("3b Auto-pause after absence", wait_until(
    lambda: engine.state == "paused", timeout=3.0))
check("3c _presence_paused marked", ctrl.is_presence_paused())

pres_stub.set_present(True)
check("3d Auto-resume on return", wait_until(
    lambda: engine.state == "running", timeout=3.0))
check("3e _presence_paused cleared", not ctrl.is_presence_paused())
cleanup(ctrl)


# =================================================================== #
#  4. 休息遮罩短/长                                                     #
# =================================================================== #

print("\n-- 4. Rest overlay short/long --")

overlay_types = []
config, engine, pres_stub, guards, stats, ctrl = build_controller()
ctrl.update_setting("rest_overlay", True)
ctrl.apply_timer_settings(25, 5, 15, 1)
win4 = m.MainWindow(ctrl)
win4.show()
pump(0.5)

def on_overlay(show, typ):
    overlay_types.append(typ)

ctrl.overlay_requested.connect(on_overlay)
ctrl.start_timer()
pump(0.3)
ctrl.skip_stage()
pump(0.3)
check("4a Work skip -> long_break (cycles=1)", engine.phase == "long_break")
check("4b Long overlay type", "long" in overlay_types,
      f"types={overlay_types}")

overlay_types.clear()
ctrl.reset_timer()
ctrl.apply_timer_settings(25, 5, 15, 2)
pump(0.3)
ctrl.start_timer()
pump(0.3)
ctrl.skip_stage()
pump(0.3)
check("4c Work skip -> short_break (cycles=2)", engine.phase == "short_break")
check("4d Short overlay type", "short" in overlay_types,
      f"types={overlay_types}")
cleanup(ctrl)


# =================================================================== #
#  5. 统计数据                                                          #
# =================================================================== #

print("\n-- 5. Statistics --")

config, engine, pres_stub, guards, stats_s, ctrl = build_controller()
win5 = m.MainWindow(ctrl)
win5.show()
pump(0.5)

stats_s.clear()
check("5a Clear -> today=0", stats_s.today_duration() == 0)
stats_s.add_work_segment(1500, done=True)
stats_s.add_work_segment(600, done=False)
check("5b Today sums segments", stats_s.today_duration() == 2100,
      f"dur={stats_s.today_duration()}")
daily = stats_s.daily_totals(7)
check("5c daily_totals(7) has 7 entries", len(daily) == 7)
check("5d Today recorded",
      daily[time.strftime('%Y-%m-%d')] == 2100)
check("5e streak counts natural tomato", stats_s.streak() == 1)
check("5f monthly non-empty", len(stats_s.monthly_totals()) > 0)
cleanup(ctrl)


# =================================================================== #
#  6. 配置持久化                                                        #
# =================================================================== #

print("\n-- 6. Config persistence --")

config, engine, pres_stub, guards, stats, ctrl = build_controller()
win6 = m.MainWindow(ctrl)
win6.show()
pump(0.5)

ctrl.apply_timer_settings(30, 5, 15, 4)
pump(0.2)
check("6a engine updated", engine.work_sec == 1800.0)
cfg = json.load(open(_tmp_config, encoding="utf-8"))
check("6b config written", cfg.get("timer", {}).get("work_min") == 30)

cfg2, eng2, ps2, g2, st2, ctrl2 = build_controller()
check("6c restart restores", eng2.work_sec == 1800.0)
cleanup(ctrl2)
cleanup(ctrl)


# =================================================================== #
#  7. 托盘 / 显隐 / 查询 / 退出                                          #
# =================================================================== #

print("\n-- 7. Tray & GUI basics --")

config, engine, pres_stub, guards, stats, ctrl = build_controller()
win7 = m.MainWindow(ctrl)
win7.show()
pump(0.5)

check("7a Tray created", win7._tray is not None)
win7.hide()
pump(0.1)
check("7b hide works", not win7.isVisible())
win7.show()
pump(0.1)
check("7c show works", win7.isVisible())
try:
    menu = win7._tray._build_menu()
    check("7d Tray menu builds", menu is not None and len(menu.actions()) >= 7)
except Exception as e:
    check("7d Tray menu builds", False, str(e))
check("7e monitor_status_text non-empty",
      len(ctrl.monitor_status_text()) > 0)
check("7f presence_status non-empty", len(ctrl.presence_status()) > 0)
lm_a, lm_e = ctrl.minimizer_status()
check("7g minimizer_status valid", isinstance(lm_a, bool))
try:
    ctrl.on_quit()
    pump(0.3)
    check("7h on_quit no exception", True)
except Exception as e:
    check("7h on_quit no exception", False, str(e))


# =================================================================== #
#  结果汇总                                                             #
# =================================================================== #

print()
print("=" * 60)
total_pass = len(_PASS)
total_fail = len(_FAIL)
if total_fail:
    print(f"Total: {total_pass} PASS, {total_fail} FAIL")
    print("Failed items:")
    for fi in _FAIL:
        print(f"  X {fi}")
    rc = 1
else:
    print(f"Total: {total_pass} PASS OK")
    rc = 0
sys.stdout.flush()
os._exit(rc)
