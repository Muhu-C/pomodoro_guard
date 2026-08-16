# -*- coding: utf-8 -*-
"""
番茄钟 · 应用监管 —— 核心逻辑层（与界面框架无关）
==================================================
包含：进程检测/关闭(ProcessManager)、番茄钟状态机(PomodoroEngine)、
配置读写(load/save_config)、进程名规范化与输出解码工具。

界面层（tkinter 版 pomodoro_guard_tk.py / PySide6 版 pomodoro_guard.py）
只负责展示与交互，通过本模块完成所有实际工作。
"""

import json
import locale
import os
import re
import subprocess
import sys
import time

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # psutil 未安装时使用 tasklist / taskkill 回退
    psutil = None
    HAS_PSUTIL = False

# 常量
PHASE_NAMES = {"work": "工作", "short_break": "短休息", "long_break": "长休息"}
PHASE_COLORS = {"work": "#e74c3c", "short_break": "#27ae60", "long_break": "#2980b9"}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # 不弹出黑窗口

# 设置文件：与程序本体同目录（exe 打包后与 exe 同目录，便于用户找到）
if getattr(sys, "frozen", False):  # PyInstaller 打包后 __file__ 指向临时目录，须用 exe 目录
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE_DIR, "pomodoro_guard_config.json")


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 名称规范化 / 输出解码
# ---------------------------------------------------------------------------
def normalize_exe(name):
    """把用户输入规范化为 exe 名：去掉路径/引号/通配符，补上 .exe。

    注意：进程名可能本身含点（如商店版 python 的进程名 pythonw3.13），
    因此只按是否以 .exe 结尾来判断，而不是按是否含点。
    """
    name = (name or "").strip().strip('"').strip("*").strip()
    name = os.path.basename(name)
    if not name.lower().endswith(".exe"):
        name += ".exe"
    return name.lower()


def proc_name_of(exe_name):
    """从规范化 exe 名得到进程名（去掉结尾的 .exe，供 Get-Process 使用）。"""
    exe_name = normalize_exe(exe_name)
    return exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name


def decode_bytes(raw):
    """按系统代码页解码子进程输出（兼容中文 Windows 的 GBK 与 UTF-8 系统）。

    中文系统上 taskkill / powershell 的输出是 GBK(CP936)，直接按 UTF-8
    解会得到乱码；这里先试 UTF-8，失败再回退到系统首选编码。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode(locale.getpreferredencoding(False))
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 进程管理：检测 / 关闭指定程序
# ---------------------------------------------------------------------------
class ProcessManager:
    """负责检测指定程序是否在运行，并强制关闭它。

    后端选择顺序：psutil(已安装时) -> PowerShell -> tasklist/taskkill。
    """

    def __init__(self):
        self.backend = self._select_backend()

    # ---- 后端选择 --------------------------------------------------------
    def _select_backend(self):
        if HAS_PSUTIL:
            try:
                next(psutil.process_iter(["name"]))
                return "psutil"
            except Exception:
                pass
        if self._powershell_ok():
            return "powershell"
        return "tasklist"

    @staticmethod
    def _powershell_ok():
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-Process -Id $PID | Out-Null"],
                capture_output=True, timeout=15,
                creationflags=CREATE_NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False

    # ---- 检测 -----------------------------------------------------------
    def is_running(self, exe_name):
        """判断名为 exe_name 的进程是否在运行（单名便捷方法）。"""
        return bool(self.find_running([exe_name]))

    def find_running(self, names):
        """一次调用批量检测 names 中哪些正在运行，返回正在运行的子集。

        各后端都尽量只生成一次子进程 / 一次迭代，避免逐个检测拖慢程序。
        """
        names = [normalize_exe(n) for n in names]
        if not names:
            return []
        if self.backend == "psutil":
            return self._find_running_psutil(names)
        if self.backend == "powershell":
            return self._find_running_powershell(names)
        return self._find_running_tasklist(names)

    def _find_running_psutil(self, names):
        want = {n.lower() for n in names}
        found = set()
        for p in psutil.process_iter(["name"]):
            try:
                nm = (p.info.get("name") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if nm in want:
                found.add(nm)
        return sorted(found)

    def _find_running_powershell(self, names):
        proc_names = ",".join("'%s'" % proc_name_of(n) for n in names)
        cmd = ("$found = Get-Process -Name {0} -ErrorAction SilentlyContinue | "
               "Select-Object -ExpandProperty ProcessName; "
               "if ($found) {{ $found }}").format(proc_names)
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, timeout=15,
                creationflags=CREATE_NO_WINDOW)
        except Exception:
            return []
        running = set()
        for line in decode_bytes(r.stdout).splitlines():
            line = line.strip().lstrip("\ufeff").lower()
            if line:
                running.add(line if line.endswith(".exe") else line + ".exe")
        want = {n.lower() for n in names}
        return sorted(running & want)

    def _find_running_tasklist(self, names):
        import csv
        import io
        try:
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15,
                creationflags=CREATE_NO_WINDOW)
            out = decode_bytes(proc.stdout)
        except Exception:
            return []
        running = set()
        try:
            for row in csv.reader(io.StringIO(out)):
                if row and row[0].strip():
                    running.add(row[0].strip().lower())
        except Exception:
            return []
        want = {n.lower() for n in names}
        return sorted(running & want)

    # ---- 关闭 -----------------------------------------------------------
    def kill(self, exe_name):
        """强制关闭 exe_name 的全部进程。

        依次尝试 taskkill(树杀) -> Stop-Process(PowerShell) -> psutil，
        直到某一步成功。返回 (是否成功, 详情说明)。
        """
        exe_name = normalize_exe(exe_name)
        attempts = []
        ok, detail = self._kill_taskkill(exe_name)
        if ok:
            return True, detail
        attempts.append(f"taskkill: {detail}")
        ok, detail = self._kill_powershell(exe_name)
        if ok:
            return True, detail
        attempts.append(f"Stop-Process: {detail}")
        if HAS_PSUTIL:
            ok, detail = self._kill_psutil(exe_name)
            if ok:
                return True, detail
            attempts.append(f"psutil: {detail}")
        return False, " | ".join(attempts)

    def _kill_taskkill(self, exe_name):
        """taskkill /T 树杀；返回 (是否成功, 详情)。"""
        try:
            proc = subprocess.run(
                ["taskkill", "/IM", exe_name, "/F", "/T"],
                capture_output=True, timeout=15,
                creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            return False, str(exc)
        out = decode_bytes(proc.stdout)
        err = decode_bytes(proc.stderr)
        if proc.returncode == 0:
            pids = re.findall(r"PID\s*(\d+)", out)
            return True, f"taskkill 成功(共{max(1, len(pids))}个)"
        if "not found" in out.lower() or "没有找到" in out or "找不到" in out:
            return False, "进程已退出(可能竞态)"
        return False, (out.strip() or err.strip() or f"taskkill 退出码 {proc.returncode}")

    def _kill_powershell(self, exe_name):
        """Stop-Process 强杀并真实验证；返回 (是否成功, 详情)。

        exit 0=已结束 1=仍在运行(输出错误) 2=未找到进程。
        """
        proc_name = proc_name_of(exe_name)
        cmd = (
            "$p = Get-Process -Name '{0}' -ErrorAction SilentlyContinue\n"
            "if (-not $p) {{ exit 2 }}\n"
            "$errs = @()\n"
            "foreach ($proc in $p) {{\n"
            "    try {{ Stop-Process -Id $proc.Id -Force -ErrorAction Stop }}\n"
            "    catch {{ $errs += $_.Exception.Message }}\n"
            "}}\n"
            "Start-Sleep -Milliseconds 250\n"
            "if (Get-Process -Name '{0}' -ErrorAction SilentlyContinue) {{\n"
            "    if ($errs.Count -gt 0) {{ Write-Output ($errs -join ' | ') }}\n"
            "    exit 1\n"
            "}}\n"
            "exit 0"
        ).format(proc_name, proc_name)
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, timeout=15,
                creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            return False, str(exc)
        out = decode_bytes(r.stdout).strip()
        if r.returncode == 0:
            return True, "Stop-Process 成功"
        if r.returncode == 2:
            return False, "进程已退出(可能竞态)"
        return False, (out or "仍在运行(可能权限不足)")

    def _kill_psutil(self, exe_name):
        """psutil 递归树杀；返回 (是否成功, 详情)。"""
        exe_lower = exe_name.lower()
        killed, errors = 0, []
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info.get("name") or "").lower() != exe_lower:
                    continue
                # 递归结束子进程树，避免留下孤儿进程
                try:
                    for child in p.children(recursive=True):
                        try:
                            child.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                except psutil.NoSuchProcess:
                    pass
                try:
                    p.kill()
                except psutil.AccessDenied:
                    p.terminate()  # 先优雅终止
                killed += 1
            except psutil.NoSuchProcess:
                continue
            except Exception as exc:
                errors.append(str(exc))
        if killed:
            return True, f"psutil 成功(共{killed}个)"
        return False, ("; ".join(errors) if errors else "未找到进程")


# ---------------------------------------------------------------------------
# 番茄钟计时器（纯逻辑，与 UI 解耦）
# ---------------------------------------------------------------------------
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
