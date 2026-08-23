# 番茄钟 · 应用监管 (Pomodoro Guard) — 模块化重构详细说明

> **文档版本**：v1.0  
> **创建日期**：2026-08-23  
> **目的**：将 `pomodoro_guard.py`（~90KB / 约 1800 行）拆分为清晰的模块架构，彻底分离 GUI 与业务逻辑，消除循环依赖，提升可维护性与可测试性。

---

## 1. 重构背景与现状

### 1.1 当前痛点
- **巨型文件**：`pomodoro_guard.py` 揉合了 UI 布局、事件绑定、摄像头规则、后台线程扫描、统计图表绘制、遮罩控制、托盘逻辑。
- **高耦合**：GUI 控件回调函数内直接操作 `PomodoroEngine`、`PresenceDetector`、`ProcessManager`，且直接读写配置，逻辑难以复用。
- **测试困难**：业务规则（例如“人离开超过 15 秒自动暂停”）与 GUI 生命周期捆绑，无法单独做单元测试。
- **多线程杂乱**：QTimer 和 QThread 与界面控件混在一起，职责不清。

### 1.2 重构目标（基于用户选型）
1. **状态机与判断** → 提升为 **总控制器 (ApplicationController)** 主导，负责所有业务规则仲裁。
2. **设置文件读写** → 保留 **即时写盘** (每次改动立即保存)，外部修改仅重启后生效（不做热加载）。
3. **摄像头逻辑** → **保持纯粹**，只输出“人在/不在”及离开时长，不做任何暂停/遮罩决策。
4. **监管逻辑** → **全部下沉**到核心层，自带独立 QThread/QTimer 自驱扫描，GUI 只需一行 `start()` / `stop()`。
5. **GUI 模块** → 彻底瘦身，**每个用户操作（点击按钮、勾选开关）在 GUI 层只调用控制器的一个方法**（一行代码）。
6. **入口** → 在 `main.py` 中完成 **所有模块实例化、依赖注入与组装**。

### 1.3 约束条件（不可违反）
- **核心层 (`core/`)** 不允许导入任何 `gui/` 模块。
- **控制器 (`controller.py`)** 可以导入 `core/` 和 `gui/`（但 gui 只做类型声明，不实例化）。
- **所有后台线程（监管扫描、摄像头采样）** 必须由核心层自管理，GUI 退出时需正确释放。
- **配置持久化** 继续使用 `pomodoro_guard_config.json`，格式不变，确保老用户平滑升级。

---

## 2. 新架构全景

### 2.1 目录树

```
pomodoro_guard/
├── core/                           # ─── 核心层 (Pure Logic, No GUI) ───
│   ├── __init__.py
│   ├── engine.py                   # ① 番茄钟状态机 (PomodoroEngine)
│   ├── config.py                   # ② 配置管理 (ConfigManager)
│   ├── presence.py                 # ③ 摄像头检测 (PresenceDetector) - 原文件微调
│   ├── guards.py                   # ④ 监管服务 (GuardianService) - 应用监管 + 全程关闭 + 工作最小化
│   ├── process_manager.py          # (从原 core 中独立) 进程查找/杀死/冷却
│   ├── stats.py                    # (保持不变) 专注时长统计 SQLite
│   └── utils.py                    # 工具函数 (GBK解码、进程名规范化等)
│
├── gui/                            # ─── 视图层 (只做显示与交互) ───
│   ├── __init__.py
│   ├── main_window.py              # ⑤ 主窗口 (仅布局、控件、样式，不含业务逻辑)
│   ├── overlay.py                  # 休息全屏遮罩 (从主窗口剥离)
│   ├── tray_icon.py                # 系统托盘 (从主窗口剥离)
│   └── widgets.py                  # 自绘组件 (柱状图、圆角按钮等)
│
├── controller.py                   # ⑥ 总控制器 (ApplicationController) - 业务规则枢纽
├── main.py                         # ⑦ 入口 (组装所有模块并启动)
├── ... (模型文件 .onnx, build_exe.bat, README.md 等保持不变)
└── pomodoro_guard_stats.db         # (运行时自动生成)
```

### 2.2 依赖方向（严格单向）

```text
main.py
   ├── 实例化 core/*
   ├── 实例化 controller (注入 core 实例)
   └── 实例化 gui/main_window (注入 controller)

controller.py
   ├── 依赖 core/* (engine, config, presence, guards, stats)
   └── 依赖 gui/* 仅作为类型提示 (不做 import 实例化)

core/*.py
   └── 相互依赖 (例如 guards 依赖 process_manager 和 config)
   └── 禁止导入 gui
```

---

## 3. 模块详细设计

### 3.1 核心层 (`core/`)

#### 3.1.1 `config.py` — 配置管理器
- **职责**：读取/写入 `pomodoro_guard_config.json`，提供默认值。
- **关键接口**：
  - `get(key, default=None)` / `set(key, value)`
  - `save()` — 即时写盘（每次 set 后自动调用，但也可显式调用）
- **约束**：设置变更后立即 `json.dump`；外部修改文件不监控。

#### 3.1.2 `engine.py` — 番茄钟状态机
- **职责**：维护当前阶段（work/short_break/long_break/stopped）、剩余秒数、周期计数；提供 `tick`、`start`、`pause`、`resume`、`skip`、`reset`。
- **信号 (PySide6.QSignal)**：
  - `state_changed(new_state: str)` — 阶段切换时发射
  - `tick(remaining: int)` — 每秒发射
- **约束**：不包含“是否自动开始”“长休息间隔”等策略判断，这些由控制器判断并调用。

#### 3.1.3 `presence.py` — 摄像头在场检测
- **职责**：后台线程采样摄像头（人脸+人体组合），每隔 `interval` 秒判定一次“人在/不在”，并累计连续不在的秒数。
- **信号**：
  - `human_status_changed(is_present: bool, absent_duration: int)` — 每次检测后发射
- **接口**：
  - `enable()` / `disable()` — 开启/关闭摄像头，引用计数安全（多次 enable 只会开一次）
- **约束**：内部自己做模型加载降级；不判断“该不该暂停”，只上报数据。

#### 3.1.4 `guards.py` — 监管服务（重点）
- **职责**：包含三个独立的扫描器，均运行在自有的 `QThread` 中：
  1. **工作监管 (WorkGuard)**：仅在 `engine.current_state == "work"` 且 `engine.is_running` 时生效。
  2. **全程关闭 (AlwaysGuard)**：从 `engine.start()` 到 `engine.reset()` 全程生效（不受阶段限制）。
  3. **工作最小化 (MinimizeGuard)**：仅在 `engine.current_state == "work"` 且 `engine.is_running` 时每 5 秒枚举窗口最小化。
- **接口**：
  - `start(scope: str)` — 启动指定扫描器 ("work" | "always" | "minimize")
  - `stop(scope: str)` / `stop_all()`
- **日志输出**：通过 `log_emitted(str)` 信号抛出，由控制器转发给 GUI。
- **约束**：扫描间隔、列表内容从 `config` 实时读取；每个扫描器有独立的失败冷却字典。

#### 3.1.5 `process_manager.py` — 进程操作
- **职责**：原 `ProcessManager` 类，提供 `find_running(prog_names)`、`kill(prog_name)`、冷却状态维护。
- **约束**：无信号，纯同步/阻塞调用（调用方需在后台线程执行）。

#### 3.1.6 `stats.py` — 统计
- **职责**：保持现有 `FocusStats` 不变。
- **新增**：提供 `add_segment(duration)`、`today_duration()`、`streak()`、`daily_totals(N)` 等方法。

---

### 3.2 总控制器 (`controller.py`)

#### 3.2.1 类定义
```python
from PySide6.QtCore import QObject, Signal

class ApplicationController(QObject):
    # ------------------- 输出信号（通知 GUI 更新） -------------------
    state_updated = Signal(str, int)          # (阶段, 剩余秒数)
    tick_updated = Signal(int)                # 每秒心跳（用于统计页实时更新）
    log_appended = Signal(str)                # 日志追加
    overlay_requested = Signal(bool, str)     # (是否显示, 类型 "short"/"long")
    stats_changed = Signal()                  # 统计页需刷新
    
    # ------------------- 初始化（依赖注入） -------------------
    def __init__(self, config, engine, presence, guards, stats):
        super().__init__()
        self._config = config
        self._engine = engine
        self._presence = presence
        self._guards = guards
        self._stats = stats
        
        # 连接核心信号到内部槽
        self._engine.state_changed.connect(self._on_engine_state_changed)
        self._engine.tick.connect(self._on_tick)
        self._presence.human_status_changed.connect(self._on_human_status_changed)
        self._guards.log_emitted.connect(self.log_appended.emit)
        
        # 初始化时恢复状态（如读取配置中的“启用监管”等）
        self._restore_state()
```

#### 3.2.2 暴露给 GUI 的公开方法（一行调用）
```python
    # 计时控制
    def start_timer(self): ...
    def pause_timer(self): ...
    def resume_timer(self): ...   # 从暂停恢复
    def skip_stage(self): ...
    def reset_timer(self): ...
    
    # 监管控制
    def toggle_guard(self, enabled: bool): ...
    def toggle_always_close(self, enabled: bool): ...
    def toggle_minimize(self, enabled: bool): ...
    
    # 配置修改（GUI 输入变化时调用，触发即时写盘）
    def update_setting(self, key, value): ...
    
    # 统计操作
    def clear_stats(self): ...
```

#### 3.2.3 内部业务规则槽（核心逻辑聚集地）
```python
    def _on_engine_state_changed(self, new_state):
        # 1. 遮罩控制（进休息开遮罩，出休息关遮罩）
        # 2. 摄像头控制（工作/休息开，暂停/停止关）
        # 3. 监管扫描器控制（工作状态启动工作监管，停止状态停全部）
        # 4. 统计落库（工作结束 -> 记录段）
        pass

    def _on_human_status_changed(self, is_present, absent_duration):
        # 专注时：不在超过阈值（配置读取） -> 自动暂停
        # 休息时：不在超过 60 秒 -> 关闭遮罩并释放摄像头
        # 注意：需判断当前 _engine 状态
        pass

    def _on_tick(self, remaining):
        # 转发 tick 信号，同时检查是否需要自动恢复（人在且暂停因无人引起）
        pass
```

---

### 3.3 GUI 层 (`gui/`)

#### 3.3.1 `main_window.py` — 主窗口（瘦身目标：< 400 行）
- **职责**：
  - `setup_ui()` — 创建所有控件（计时区、设置页、统计页），应用深色主题 & 云母。
  - 连接控件信号（按钮点击、复选框、输入框）到 `self.controller` 的对应方法。
  - 连接 `controller` 的信号到 UI 更新方法（`_update_display`、`_append_log`、`_update_stats`）。
- **禁止**：
  - 直接访问 `PomodoroEngine`、`PresenceDetector`、`Guards` 等核心类。
  - 直接读写 `config.json`。
  - 编写任何 if-else 业务判断（如“如果人离开则暂停”）。

#### 3.3.2 `overlay.py` 与 `tray_icon.py`
- **剥离**：从 `main_window.py` 中独立出来，作为纯 GUI 组件。
- **交互**：只响应 `controller.overlay_requested` 信号来显示/隐藏；托盘菜单的“开始/暂停/退出”全部调用 `controller` 方法。

---

### 3.4 入口 (`main.py`)

```python
import sys
from PySide6.QtWidgets import QApplication
from core.config import ConfigManager
from core.engine import PomodoroEngine
from core.presence import PresenceDetector
from core.guards import GuardianService
from core.stats import FocusStats
from controller import ApplicationController
from gui.main_window import MainWindow
from gui.tray_icon import TrayIcon

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 托盘常驻
    
    # 1. 实例化核心层（无 GUI 依赖）
    config = ConfigManager()
    engine = PomodoroEngine(config)
    stats = FocusStats()
    presence = PresenceDetector()
    guards = GuardianService(config)  # 内部自动起线程
    
    # 2. 组装总控制器（注入所有核心对象）
    controller = ApplicationController(config, engine, presence, guards, stats)
    
    # 3. 组装 GUI（注入控制器）
    window = MainWindow(controller)
    tray = TrayIcon(controller, window)  # 托盘也需要控制器
    
    # 4. 程序退出时确保线程释放
    app.aboutToQuit.connect(guards.stop_all)
    app.aboutToQuit.connect(presence.disable)
    
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
```

---

## 4. 迁移步骤（实施检查清单）

| 步骤 | 任务 | 产出 |
|---|---|---|
| 1 | 新建 `core/`、`gui/` 目录，创建 `__init__.py` | 目录骨架 |
| 2 | 将原 `pomodoro_core.py` 拆分为 `core/engine.py`、`core/config.py`、`core/process_manager.py`、`core/utils.py` | 核心工具类 |
| 3 | 将原 `presence.py` 移动至 `core/presence.py`，检查信号是否完整 | 摄像头模块 |
| 4 | 将原 `stats.py` 移动至 `core/stats.py` | 统计模块 |
| 5 | **新建 `core/guards.py`**：将原 `pomodoro_guard.py` 中的所有 `QTimer` 扫描逻辑迁移至此，重构为三个独立扫描器 | 监管服务 |
| 6 | **新建 `controller.py`**：将所有业务规则（if-else 判断）从 GUI 回调中提取至此 | 控制器 |
| 7 | 重构 `pomodoro_guard.py` → `gui/main_window.py`：移除所有逻辑，只保留 UI 布局和信号绑定 | 瘦身主窗口 |
| 8 | 剥离 `gui/overlay.py`、`gui/tray_icon.py`、`gui/widgets.py` | 辅助 GUI 组件 |
| 9 | **新建 `main.py`**：写入依赖注入与组装代码 | 新入口 |
| 10 | 删除旧 `pomodoro_guard.py`，更新 `build_exe.bat` 入口点为 `main.py` | 收尾 |
| 11 | 运行 `python main.py`，逐个验证：番茄钟切换、监管关闭、摄像头自动暂停、遮罩显示、统计实时更新 | 功能回归 |

---

## 5. 风险提示与注意事项

1. **线程安全**：`guards.py` 中的扫描器运行在 `QThread`，内部调用 `process_manager.kill()` 为阻塞操作，切勿在 GUI 线程直接调用。所有 `config.get()` 读取需注意并发（配置变更只在主线程触发，但扫描线程只读，可接受）。
2. **信号连接**：`controller` 与 `main_window` 的信号/槽必须使用 `Qt.AutoConnection`（默认），跨线程信号自动排队，无需手动加锁。
3. **配置即时写盘**：`ConfigManager.set()` 内调用 `save()` 是同步 I/O，如果 GUI 高频修改（如拖动滑块），可能短暂卡顿。若出现此情况，可在控制器中做防抖（debounce），但预期现有设置项数量（< 20 个）不会造成问题。
4. **旧配置文件兼容**：`ConfigManager` 读取时若字段缺失，使用默认值填充并保存，确保老用户升级不丢失数据。
5. **单例锁**：仍放在 `main.py` 的 `QLockFile`，跨模块不共享锁文件逻辑。

---

## 6. 验收标准

重构完成后，以下场景必须与旧版本行为完全一致（回归测试用例）：
- [ ] 点击“开始” → 计时倒计时，进入工作阶段监管生效（若启用）。
- [ ] 点击“暂停” → 倒计时暂停，摄像头检测仍在后台运行（若已启用）。
- [ ] 人离开摄像头超 15 秒 → 自动暂停；人回来 → 自动恢复。
- [ ] 进入短休息/长休息 → 自动全屏遮罩，短休和长休内容不同。
- [ ] 休息时人离开超 60 秒 → 遮罩关闭，本次休息不再重开。
- [ ] “应用监管”列表中的进程在工作阶段被杀，休息阶段不杀。
- [ ] “全程自动关闭”列表中的进程在包括休息、暂停在内的任何时间被杀。
- [ ] “工作时段最小化”列表中的进程在工作运行时被最小化，暂停/休息时不触发。
- [ ] 统计页“今日/本周/本月/累计”数字与柱状图显示正常。
- [ ] 展开设置页修改任意项 → 配置即时保存，重启读取正确。
- [ ] 托盘菜单“显示/隐藏”、“开始/暂停”、“跳过”、“重置”、“退出”全部正常。
