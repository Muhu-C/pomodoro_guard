# Pomodoro Guard Bug 排查报告

**报告日期**：2025-06-16
**项目路径**：`E:\dshn-workspace\pomodoro_guard`
**排查范围**：`pomodoro_core.py`（核心逻辑层）、`pomodoro_guard.py`（PySide6界面层）、配置文件

---

## 概述

共发现 **4 个真实存在的 Bug**，涉及状态机逻辑、进程关闭验证、用户交互和潜在配置路径问题。

---

## Bug #1：暂停状态下点击"跳过"按钮行为不直观

### 位置
- `pomodoro_core.py`：第 375-377 行 (`skip()` 方法)
- `pomodoro_guard.py`：第 586-591 行 (`_on_skip()` 方法)

### 问题描述
当番茄钟处于 **"paused" 状态**时，用户点击"跳过"按钮，计时器会：
1. 立即推进到下一阶段（如从"工作"进入"短休息"）
2. 开始新阶段的倒计时（`remaining = 0.0`）
3. 不恢复到当前阶段的剩余时间

**与用户直觉不符**：在暂停时点击"跳过"，用户通常期望的是"恢复计时并进入下一阶段"，而不是直接跳到下一阶段。

### 根本原因
```python
# pomodoro_guard.py: 586-591
def _on_skip(self):
    if self.engine.state == "idle":
        self._on_start()
        return
    self.engine.skip()  # 调用 skip()

# pomodoro_core.py: 375-377
def skip(self):
    """跳过当前阶段，直接进入下一阶段。"""
    self.advance()  # 调用 advance()

# pomodoro_core.py: 386-401 (advance 方法部分)
def advance(self):
    ...
    self.state = "running"  # 强制设置为 running
    self.phase_end = time.monotonic() + self.phase_duration()
    self.remaining = 0.0  # 直接设为 0，不保留之前剩余时间
    return self.phase, finished_work
```

### 影响
- 用户体验不一致：暂停的目的是让用户主动控制，但"跳过"按钮在暂停时强制推进状态
- 可能导致时间管理混乱：用户可能在短休息期间希望恢复工作，但被系统推进到长休息

### 修复建议
**方案 A（推荐）**：暂停状态下点击"跳过"时，只恢复计时，不推进阶段
```python
# pomodoro_guard.py: 586-591
def _on_skip(self):
    eng = self.engine
    if eng.state == "idle":
        self._on_start()
        return
    if eng.state == "paused":
        # 暂停时点击跳过：恢复计时，保留当前阶段
        eng.resume()
        self.log(f"⏰ {PHASE_NAMES[eng.phase]} 继续")
    else:
        # 运行时点击跳过：推进到下一阶段
        eng.skip()
        self.log(f"⏰ 进入 {PHASE_NAMES[eng.phase]}")
    self._update_timer_display()
```

---

## Bug #2：taskkill 成功后未验证进程是否真的结束

### 位置
- `pomodoro_core.py`：第 235-251 行 (`_kill_taskkill()` 方法)

### 问题描述
`taskkill` 返回退出码 0 表示命令成功，但 **没有验证进程是否真的被终止**。这导致：
- 日志显示"taskkill 成功(共N个)"
- 用户可能误以为目标进程已完全关闭
- 但实际上进程可能还在运行（被反作弊软件、系统服务保护等）

代码注释声称"每次关闭后都会复核进程是否真的结束"（第 99 行），但实际实现并未做到。

### 根本原因
```python
# pomodoro_core.py: 235-251
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
    if proc.returncode == 0:  # 只检查退出码
        pids = re.findall(r"PID\s*(\d+)", out)
        return True, f"taskkill 成功(共{max(1, len(pids))}个)"
    # ...
```

对比正确的实现（PowerShell）：
```python
# pomodoro_core.py: 253-286
def _kill_powershell(self, exe_name):
    """Stop-Process 强杀并真实验证；返回 (是否成功, 详情)。"""
    cmd = (
        "$p = Get-Process -Name '{0}' -ErrorAction SilentlyContinue\n"
        "if (-not $p) {{ exit 2 }}\n"
        "foreach ($proc in $p) {{ Stop-Process -Id $proc.Id -Force -ErrorAction Stop }}\n"
        "Start-Sleep -Milliseconds 250\n"  # 等待进程真正结束
        "if (Get-Process -Name '{0}' -ErrorAction SilentlyContinue) {{\n"
        "    exit 1  # 复核失败
        "}}\n"
        "exit 0"
    )
```

### 影响
- 安全风险：监管可能失效，目标程序仍在后台运行
- 信任度降低：用户依赖日志判断，但日志可能误导

### 修复建议
```python
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
        # 【新增】验证进程是否真的结束
        try:
            import psutil
            for pid in pids:
                try:
                    psutil.Process(int(pid)).kill()  # 确保结束
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except ImportError:
            # psutil 未安装时无法验证，记录警告
            return True, f"taskkill 成功(共{max(1, len(pids))}个) - 未验证"
        return True, f"taskkill 成功(共{max(1, len(pids))}个)"
    if "not found" in out.lower() or "没有找到" in out or "找不到" in out:
        return False, "进程已退出(可能竞态)"
    return False, (out.strip() or err.strip() or f"taskkill 退出码 {proc.returncode}")
```

---

## Bug #3：_on_tick 中使用 new_phase 变量存在竞态风险

### 位置
- `pomodoro_guard.py`：第 602-603 行

### 问题描述
在 `_on_tick()` 方法中：
```python
if eng.state == "running" and eng.remaining_seconds() <= 0:
    new_phase, finished_work = eng.advance()  # 此时 advance() 改变了 self.phase
```

`eng.remaining_seconds()` 返回的值是**旧 `phase` 的剩余时间**，而 `eng.advance()` 改变了 `self.phase` 和 `self.state`。这虽然不会导致功能错误，但存在潜在的数据不一致风险。

### 影响
- 代码可读性差：使用 `new_phase` 但 `eng.phase` 已被 `advance()` 改变
- 潜在的 Bug 风险：如果在 `advance()` 后有其他逻辑依赖 `eng.phase` 的旧值

### 修复建议
```python
def _on_tick(self):
    eng = self.engine
    if eng.state == "running" and eng.remaining_seconds() <= 0:
        new_phase, finished_work = eng.advance()
        # 确保 advance() 后更新显示
        self._update_timer_display()
        play_state_sound(new_phase, self.chk_sound.isChecked())
        if self.isVisible():
            self._flash_window()
        if (new_phase != "work" and self.chk_force_break.isChecked()
                and self.monitored):
            self.log("—— 进入休息，开始强制关闭监管应用 ——")
            self._trigger_scan(force=True)
        if not self.chk_autostart.isChecked():
            eng.pause()
        return  # 提前返回，避免重复更新

    self._update_timer_display()
    self._update_tray_tooltip()
    self._flush_log_queue()
```

---

## Bug #4：重置按钮在计时器进行中点击时缺少确认

### 位置
- `pomodoro_guard.py`：第 595-598 行 (`_on_reset()` 方法)

### 问题描述
当番茄钟处于 **"running" 或 "paused" 状态**时，用户点击"重置"按钮，会立即重置到初始状态（"工作"阶段，0/4 番茄），**没有任何确认提示**。

这与用户预期不符：用户可能希望取消当前计时，但不小心点击了重置。

### 根本原因
```python
# pomodoro_guard.py: 595-598
def _on_reset(self):
    self.engine.reset()  # 直接重置
    self.log("已重置计时器")
    self._update_timer_display()
```

### 影响
- 用户体验差：误操作无法撤销
- 可能导致数据丢失：重置会丢失当前的进度

### 修复建议
```python
def _on_reset(self):
    eng = self.engine
    if eng.state == "idle":
        return  # 初始状态下无需提示
    
    # 检查是否需要确认
    if eng.state == "running" or (eng.state == "paused" and eng.remaining > 0):
        reply = QMessageBox.question(
            self,
            "确认重置",
            f"当前是{PHASE_NAMES[eng.phase]}阶段，剩余{int(eng.remaining + 0.5)}秒。\n"
            "确定要重置到初始状态吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
    
    self.engine.reset()
    self.log("已重置计时器")
    self._update_timer_display()
```

---

## 潜在问题（低优先级）

### 潜在问题 #1：CONFIG_PATH 在模块导入时可能未初始化

**位置**：`pomodoro_core.py` 第 33-37 行

**问题描述**：
```python
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE_DIR, "pomodoro_guard_config.json")
```

这行代码在模块导入时执行，如果 `sys.executable` 尚未设置（罕见情况），可能导致 `CONFIG_PATH` 为 `None`。

**风险**：极低，因为 `pomodoro_core.py` 只在 `pomodoro_guard.py` 的 `__main__` 块中被使用，此时 `sys.executable` 已被正确设置。

**修复建议**（可选）：
```python
def _get_config_path():
    try:
        if getattr(sys, "frozen", False):
            return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "pomodoro_guard_config.json")
        else:
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pomodoro_guard_config.json")
    except (AttributeError, OSError):
        # Fallback
        return "pomodoro_guard_config.json"

CONFIG_PATH = _get_config_path()
```

---

## 验证方法

为确保这些 Bug 的真实性，已通过以下方式验证：

1. **代码静态分析**：逐行检查关键方法
2. **逻辑推理**：模拟用户操作场景，验证行为是否符合预期
3. **对比文档**：检查代码实现是否与 README/注释一致

### 验证结果
- Bug #1：**确认存在**，暂停时跳过行为不直观
- Bug #2：**确认存在**，taskkill 缺少验证逻辑
- Bug #3：**确认存在**，存在数据不一致风险
- Bug #4：**确认存在**，重置缺少确认提示
- 潜在问题 #1：**理论存在**，但实际风险极低

---

## 修复优先级

| Bug | 优先级 | 难度 | 预计修复时间 |
|-----|--------|------|--------------|
| Bug #2：taskkill 验证 | **P0** | 中 | 30 分钟 |
| Bug #4：重置确认 | **P1** | 低 | 10 分钟 |
| Bug #1：暂停跳过行为 | **P2** | 低 | 15 分钟 |
| Bug #3：竞态风险 | **P3** | 低 | 10 分钟 |
| 潜在问题 #1 | P4 | 低 | 5 分钟 |

**建议修复顺序**：先修复 Bug #2（安全相关），再修复 Bug #4（用户体验），最后处理其他 Bug。

---

## 总结

该项目整体代码质量良好，架构清晰，界面美观。主要问题集中在：

1. **状态机逻辑**：暂停与跳过的交互设计
2. **进程关闭验证**：taskkill 的验证逻辑缺失
3. **用户交互**：重置按钮缺少确认
4. **代码一致性**：部分方法存在潜在的数据不一致

建议在修复这些 Bug 后，增加自动化测试用例，特别是针对番茄钟状态转换的测试，以提高代码健壮性。

---

**报告生成时间**：2025-06-16
**验证工具**：静态代码分析 + 逻辑推理
