# -*- coding: utf-8 -*-
"""进程检测与强制关闭 (ProcessManager)。"""

import re
import subprocess

from .utils import (CREATE_NO_WINDOW, HAS_PSUTIL, decode_bytes, normalize_exe,
                    proc_name_of, psutil)


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
        任何后端异常都按"无运行"处理并返回空列表，不中断扫描周期。
        """
        names = [normalize_exe(n) for n in names]
        if not names:
            return []
        try:
            if self.backend == "psutil":
                return self._find_running_psutil(names)
            if self.backend == "powershell":
                return self._find_running_powershell(names)
            return self._find_running_tasklist(names)
        except Exception:
            return []  # 检测失败兜底：本次扫描按"无运行"处理

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
        # 进程名中的单引号按 PowerShell 字符串规则转义（''），防止命令损坏
        proc_names = ",".join("'%s'" % proc_name_of(n).replace("'", "''")
                              for n in names)
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
        # 单引号转义，防止进程名含 ' 时命令语法损坏
        proc_name = proc_name_of(exe_name).replace("'", "''")
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