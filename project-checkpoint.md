# 番茄钟·应用监管（Pomodoro Guard）项目快照

> 生成日期：2026-08-23 · 状态：模块化重构完成（`core/` + `gui/` + `controller.py` + `main.py` 分层，依赖注入组装），56 项全栈自动化回归全部通过；摄像头在场检测、数据统计、三份监管列表齐备

---

## 1. 项目目标

| 层面 | 目标 |
|---|---|
| 功能 | ① 番茄钟（工作/短休息/长休息周期）② 应用监管（工作阶段强制关闭指定程序）③ 全程应用自动关闭（不受阶段限制全程关闭）④ 工作时段应用自动最小化（工作阶段自动最小化指定窗口）⑤ 摄像头在场检测（离位自动暂停/回位恢复）＋ 专注时长统计（日/周/月/累计 + streak） |
| 技术 | Python + PySide6 桌面应用；**分层架构**（`core/` 纯逻辑、`gui/` 视图、`controller.py` 业务规则、`main.py` 组装）；PyInstaller 打包为免安装 exe（目标机器零 Python 运行时） |
| 体验 | 深色主题、Win11 云母背景、系统托盘常驻、单例运行、配置自动持久化、阶段切换提示音、数据统计可视化 |
| 质量 | 界面与核心逻辑分层（可单测）；每轮功能改动均带自动化验证；崩溃/异常优雅回退 |

---

## 2. 模块架构（分层）

依赖方向严格单向：`main.py → controller.py → core/*`；`gui/*` 只由 `main.py` 实例化、被 `controller` 引用（仅类型提示，不实例化）；**`core/` 禁止导入任何 `gui/` 模块**。所有后台线程（监管扫描、摄像头采样）由核心层自管理，GUI 退出时正确释放。

### 2.1 核心逻辑层 `core/`（UI 无关，可单测）

| 文件 | 行数 | 职责 |
|---|---|---|
| `core/engine.py` | ~85 | 番茄钟状态机（work/short_break/long_break/stopped、monotonic 防漂移）；不含"是否自动开始"等策略判断，由控制器判断并调用 |
| `core/config.py` | ~60 | `ConfigManager`：get/set/save/reload，`threading.RLock` 线程安全，`set()` 即时写盘；旧 `load_config`/`save_config` 保留向后兼容 |
| `core/presence.py` | ~255 | 摄像头人像在场检测（人脸 + 人体组合，后台线程），只上报"人在/不在 + 连续不在时长"，**不做任何暂停/遮罩决策** |
| `core/guards.py` | ~230 | `GuardianService`：三个独立扫描器（WorkGuard 工作监管 / AlwaysGuard 全程关闭 / MinimizeGuard 工作最小化），各自独立 QThread + QTimer 自驱 |
| `core/process_manager.py` | ~210 | 进程批量检测（后端自动选择 psutil → PowerShell → tasklist/taskkill）、多策略强杀、失败冷却、复核不谎报 |
| `core/stats.py` | ~135 | 专注时长统计（SQLite 段记录、日/周/月聚合、streak） |
| `core/window_minimize.py` | ~130 | 工作时段最小化应用（EnumWindows + GetWindowThreadProcessId + ShowWindow，幂等，自身 PID 窗口跳过） |
| `core/utils.py` | ~50 | 工具函数与共享常量：`normalize_exe`/`proc_name_of`/`decode_bytes`（GBK/UTF-8 自适应）、`PHASE_NAMES`、`PHASE_COLORS`、`CREATE_NO_WINDOW`、psutil 探测 |

**core/guards.py 设计要点**：
- 三个扫描器继承 `_BaseGuard`，各自运行在独立 QThread 事件循环，QTimer 周期触发（首次启动立即扫描一次）；扫描间隔/列表从 config 实时读取，间隔变化立即重设定时器
- 启动/停止/强制清场经 `QMetaObject.invokeMethod(..., Qt.QueuedConnection)` 跨线程调用（均为 `@Slot()`）
- `force_scan`（进入休息强制清场）绕过启用开关与 60s 失败冷却
- 每个扫描器有独立失败冷却字典；统一 `log_emitted(str)` 信号由控制器转发给 GUI

### 2.2 总控制器 `controller.py`（~545 行，业务规则枢纽）

- **职责**：GUI 的每个用户操作（点击按钮、勾选开关）只调用控制器的一个方法（一行代码）；控制器连接核心信号，内部维护所有 if-else 业务判断。
- **输出信号**：`tick_updated`（200ms 心跳）、`phase_changed`（阶段切换，GUI 据此播提示音）、`log_appended`（日志）、`overlay_requested`（遮罩请求，GUI 负责创建/销毁遮罩实例）、`stats_changed`（统计页刷新）、`presence_alert`（摄像头降级提示）。
- **公开方法**：计时（start_timer/pause_timer/resume_timer/skip_stage/reset_timer/apply_timer_settings）、监管三开关（toggle_guard/toggle_always_close/toggle_minimize）、三份列表增删清（add/remove/clear monitored/always/minimize apps）、通用 `update_setting(key, value)`、`toggle_presence`/`toggle_rest_overlay`、`clear_stats`、`on_quit`；查询：`monitor_status_text`/`presence_status`/`live_focus_extra`/`enforcement_active`/`always_close_active`/`minimize_active`。
- **内部规则**（从旧 GUI 回调提取）：`_on_tick`（200ms 轮询引擎状态、检测阶段自然结束）、`_on_phase_transition`（阶段切换统一处理：遮罩同步 + 强制清场 + 监管调度）、`_check_presence_rules`（专注离开超宽限→自动暂停；人回来→自动恢复；休息无人超 60s→关遮罩）、`_update_presence`（按状态幂等启停摄像头）、`_sync_overlay`、`_sync_guards_for_state`（按引擎状态+配置启停扫描器）、`_log_work_segment`（工作段落库）。
- **关键设计**：`_presence_paused` 标记区分"手动暂停"与"离开暂停"（手动暂停不被摄像头自动恢复）；提示音职责分离（控制器只发 `phase_changed`，GUI 按 `sound_enabled` 配置播放）；遮罩请求式架构（控制器不持有遮罩实例）；重置时 `_stop_all_guards` 仅停定时器、程序退出才 `GuardianService.stop_all()` 退出线程。

### 2.3 界面层 `gui/`（只做显示与交互，不含业务判断）

| 文件 | 行数 | 职责 |
|---|---|---|
| `gui/main_window.py` | ~825 | 主窗口：布局、控件、信号绑定、深色主题/云母、收起/展开双模式、设置区滚动；内部自建系统托盘 |
| `gui/overlay.py` | ~90 | 休息全屏遮罩（短/长不同文本、多屏联合几何、鼠标/键盘事件拦截） |
| `gui/tray_icon.py` | ~190 | 系统托盘（QPainter 自绘番茄图标、左键显隐、右键状态菜单、tooltip） |
| `gui/widgets.py` | ~235 | 自绘柱状图 BarChart、统计面板 StatsPanel（四档+streak+清除）、format_duration |
| `gui/theme.py` | ~250 | 深色 QSS 与云母变体、阶段配色 DARK_PHASE_COLORS、supports_mica/apply_mica、内存合成提示音 play_state_sound、尺寸常量 |

**禁则**：GUI 不直接访问 `PomodoroEngine`/`PresenceDetector`/`GuardianService` 等核心类，不直接读写 config，不编写业务 if-else 判断。

### 2.4 入口 `main.py`（~60 行）

组装顺序：`QApplication` + 单例锁（`acquire_lock`，第二实例弹提示退出、崩溃残留锁自动清理）→ 实例化 core/*（无 GUI 依赖）→ `ApplicationController`（注入 config/engine/presence/guards/stats）→ `MainWindow(controller)`（内部自建托盘）→ show → `app.exec()`。退出清理由托盘「退出」/「以管理员身份重启」两条显式路径调用 `controller.on_quit()`（落库 + 释放摄像头 + 停止扫描器线程）；不挂 `aboutToQuit`（`on_quit` 中段落落库非幂等，重复调用会重复写库）。

### 2.5 工具与资源

| 文件 | 说明 |
|---|---|
| `make_icon.py` | 用 QPainter 生成番茄 `icon.ico`（复用托盘图标绘制） |
| `build_exe.bat` | 一键打包（onedir+windowed，入口 `main.py`，自动定位依赖目录、依赖自检、打包两个摄像头模型） |
| `requirements.txt` | PySide6（必需）、psutil/opencv（可选） |
| `face_detection_yunet_2023mar.onnx` | YuNet 人脸检测模型（~230KB） |
| `person_det.onnx` | YOLOv8n 人体检测模型（~12MB，COCO person 类，低头/背对镜头兜底） |
| `icon.ico` | exe/窗口/托盘图标 |
| `README.md` | 架构、安装、打包、使用说明 |

### 2.6 运行时产物

| 文件 | 说明 |
|---|---|
| `pomodoro_guard_config.json` | 自动生成：监管列表/间隔/时长/全部开关（**格式未变，老用户平滑升级**） |
| `pomodoro_guard_stats.db` | 自动生成：专注时长统计库（SQLite，勿手动编辑） |
| `dist/PomodoroGuard/` | 交付物：`PomodoroGuard.exe`＋`_internal\`（免安装，整文件夹分发） |

---

## 3. 已完成功能清单

**番茄钟**
- [x] 工作时长/短休息/长休息/长休间隔可配置（分两行布局，勾选项各占一行）
- [x] 开始 / 暂停·继续 / 跳过 / 重置；阶段结束自动切换（可关）
- [x] 窗口总在最前（勾选即时生效）；状态栏显示阶段/计数/监管状态
- [x] 阶段切换提示音（工作=上行双音、短休=下行双音、长休=三连音；自动切换与手动「跳过」均触发）
- [x] 休息全屏遮罩提醒（进入休息强制全屏，仅主窗口悬浮其上；短休息/长休息独立显示内容：短休息显示"请立即看向6米以外的窗外物体，并眨眼10次！"，长休息额外显示"请拿起水杯到厨房倒一杯水"喝水提示；可勾选关闭，设置持久化）
- [x] 主窗口收起/展开双模式（打开默认收起；进入休息自动收起并移右上角；程序不自动展开）

**摄像头在场检测**
- [x] 人脸+人体组合：低头/背对镜头不误判；摄像头按需启停、双模型独立降级；状态栏显示状态
- [x] 专注离开超宽限自动暂停/人回来自动恢复，手动暂停不被自动恢复
- [x] 休息无人超1分钟关遮罩且本次不重开

**数据统计**
- [x] 专注总时长实时累计：今日/本周/本月/累计四档
- [x] 自绘柱状图+悬停提示+柱顶标注
- [x] streak 连续天数按自然结束番茄计
- [x] SQLite 实时落库、退出不丢；清除二次确认
- [x] 展开模式两页导航「设置·监管/统计」，展开默认统计页

**应用监管**
- [x] 指定程序批量检测＋多策略强制关闭＋结果复核（不谎报成功）
- [x] 固定仅"工作"阶段扫描（休息/暂停/未开始不关闭）；如需全时段关闭可使用「全程应用自动关闭」
- [x] 可选"进入休息时强制关闭"；60s 失败冷却；权限不足/反作弊提示
- [x] GBK 日志正确解码，带时间戳

**全程应用自动关闭**
- [x] 独立列表（`always_closed`），番茄钟始终扫描（不受阶段限制）；与监管列表共享后台线程但独立冷却/日志标识（`[全程关闭]`）；启用开关与列表均持久化

**工作时段应用自动最小化**
- [x] 独立列表（`work_minimized`），仅工作阶段计时运行中（非暂停）生效，固定 5s 检测
- [x] 按进程名枚举可见顶层窗口，仅最小化未最小化者（幂等，用户恢复后再次最小化）
- [x] 同 PID 自身窗口跳过（主窗口/休息遮罩不受影响）
- [x] 独立 `_minimize_busy` 线程标记、日志标识 `[工作最小化]`（仅命中时记录防刷屏）
- [x] 启用开关与列表均持久化；pywin32/psutil 缺失自动降级

**界面与系统**
- [x] 深色主题（全控件显式配色）
- [x] Win11 22H2+ 云母背景（与标题栏同材质），旧系统自动回退
- [x] 系统托盘（显隐/状态菜单/悬浮提示/关闭隐藏）
- [x] 单例保护＋崩溃恢复（残留锁自动清理）
- [x] 设置持久化（监管列表/全程关闭列表/间隔/时长/全部开关）
- [x] 以管理员身份重启（UAC，兼容打包后 exe）
- [x] 「设置·监管」页支持纵向滚动（QScrollArea 包裹，禁用横向滚动条）；番茄钟设置分组控件间距增大至 11px（≈180% 高度）

**打包与工程**
- [x] PyInstaller onedir 免安装 exe（无需 Python 运行时；排除未用 Qt 模块减体积）
- [x] 一键打包脚本（自动定位依赖、依赖自检、自动生成图标）
- [x] **模块化分层重构**：单体 `pomodoro_guard.py`（~1800 行）拆分为 `core/`（纯逻辑）＋ `gui/`（视图）＋ `controller.py`（业务规则）＋ `main.py`（依赖注入组装），依赖方向单向、core 不导入 gui（详见 §5）
- [x] 核心/界面分层，每轮改动带 offscreen＋真实窗口自动化验证（重构后累计 158 项回归：_step7 36 + _step8 53 + _step9 16 + _step11 56）

---

## 4. 已知 Bug / 限制

### 待修复 Bug

| 项 | 说明 |
|---|---|
| **中文路径导致摄像头检测失效** | 软件目录含中文（如 `桌面\番茄钟`）时：`os.path.exists` 判定模型存在，但 `cv2.FaceDetectorYN.create`/`cv2.dnn.readNetFromONNX` 内部用窄字符 fopen，无法打开中文路径 → 人脸/人体双模型加载失败 → 摄像头已打开但检测永不工作（灯亮、Windows 显示"正在使用摄像头"、状态栏"不可用"）。**规避**：目录改纯英文；**根治**：`core/presence.py` 加载前把模型复制到 ASCII 临时目录再交给 cv2 |
| **模型加载失败时摄像头句柄泄漏** | `core/presence.py` 的 `_run()` 在 `_acquire()` 返回 False 的失败分支直接 return，未调用 `_release()` → 摄像头保持打开（灯常亮、被占用）；成功路径由 finally 释放，仅失败路径泄漏。**修复**：失败分支补 `_release()` |

### 设计限制与已知约束

| 项 | 说明 |
|---|---|
| 反作弊保护进程（原神 mhyprot2）无法强杀 | taskkill/Stop-Process 均"拒绝访问"，提权也不行；用户态无解，日志已提示 |
| 目标以管理员运行且本程序未提权 | 关闭失败；需点「以管理员身份重启」 |
| 打包时 exe 正在运行 | 打包因 DLL 占用失败；需先关闭软件再打包 |
| onedir 分发 | 必须整文件夹拷贝，单独拷 exe 无法运行 |
| 设置文件不可写 | 降级默认值，会话内仍有效，日志提示 |
| 云母仅 Win11 22H2+ | Win10 无云母，自动回退纯深色 |
| 系统托盘不可用（个别环境） | 关闭按钮退回"询问退出" |
| 摄像头检测的固有限制 | 人脸/人体检测均受光照、遮挡与摄像头视角影响；YOLOv8n 需 CPU 推理（640x640，约 0.1-0.3 秒/次）；多摄像头仅选第一个；摄像头启用时指示灯常亮；依赖 opencv 与两个模型文件，缺失其一则只用另一个 |
| 不装 pywin32 | 「工作时段最小化应用」不可用，启动日志提示，其余功能不受影响 |

---

## 5. 模块化重构实施记录（迁移自原 Project_Upgrade.md）

### 5.1 背景与目标

- **痛点**：单体 `pomodoro_guard.py`（~90KB/约 1800 行）揉合 UI 布局、事件绑定、摄像头规则、后台线程扫描、统计图表、遮罩控制、托盘逻辑；GUI 回调直接操作 `PomodoroEngine`/`PresenceDetector`/`ProcessManager` 并直接读写配置；业务规则（如"人离开超 15 秒自动暂停"）与 GUI 生命周期捆绑，无法单独单测；多线程职责不清。
- **目标**（用户选型）：① 状态机与判断上收为总控制器（ApplicationController）主导；② 配置保持即时写盘（外部修改仅重启后生效，不做热加载）；③ 摄像头保持纯粹（只输出"人在/不在"及离开时长，不做暂停/遮罩决策）；④ 监管逻辑全部下沉核心层，自带 QThread/QTimer 自驱扫描，GUI 一行 start/stop；⑤ GUI 彻底瘦身，每个用户操作只调用控制器一个方法；⑥ 入口 `main.py` 完成所有模块实例化、依赖注入与组装。
- **约束（不可违反）**：`core/` 不允许导入任何 `gui/` 模块；`controller.py` 可导入 core 和 gui（gui 仅类型声明，不实例化）；所有后台线程由核心层自管理，GUI 退出时正确释放；配置持久化继续使用 `pomodoro_guard_config.json`，格式不变，老用户平滑升级。

### 5.2 迁移检查清单（11 步全部完成）

| 步骤 | 任务 | 产出 | 状态 |
|---|---|---|---|
| 1 | 新建 `core/`、`gui/` 目录与 `__init__.py` | 目录骨架 | ✅ |
| 2 | 原 `pomodoro_core.py` 拆分为 `core/engine.py`、`config.py`、`process_manager.py`、`utils.py` | 核心工具类 | ✅ |
| 3 | 原 `presence.py` → `core/presence.py`（微调模型定位：非冻结态用 dirname(dirname(__file__)) 回溯项目根） | 摄像头模块 | ✅ |
| 4 | 原 `stats.py` → `core/stats.py`（微调统计库路径定位，位置与重构前一致） | 统计模块 | ✅ |
| 5 | 新建 `core/guards.py`：三个独立扫描器（WorkGuard/AlwaysGuard/MinimizeGuard），`core/config.py` 新增 `ConfigManager`（RLock 线程安全），`core/window_minimize.py` 从顶层迁移 | 监管服务 | ✅ |
| 6 | 新建 `controller.py`：所有业务规则（if-else 判断）从 GUI 回调提取至此 | 控制器 | ✅ |
| 7 | 原 `pomodoro_guard.py` → `gui/main_window.py`：移除所有逻辑，只留 UI 布局和信号绑定（瘦身目标 <400 行未达成，约 830 行——四组设置区布局代码本身较长；"移除业务逻辑"目标已达成） | 瘦身主窗口 | ✅ |
| 8 | 剥离 `gui/overlay.py`、`tray_icon.py`、`widgets.py`、`theme.py` | 辅助 GUI 组件 | ✅ |
| 9 | 新建 `main.py`：依赖注入与组装 | 新入口 | ✅ |
| 10 | 删除旧顶层 `pomodoro_guard.py`/`pomodoro_core.py`/`presence.py`/`stats.py`/`window_minimize.py`；`build_exe.bat`/`PomodoroGuard.spec` 入口点改为 `main.py`；引用完整性核验无残留 import | 收尾 | ✅ |
| 11 | 功能回归：番茄钟切换、监管关闭、摄像头自动暂停、遮罩显示、统计实时更新（56 项全通过） | 回归 | ✅ |

### 5.3 实施中修复的关键问题（步骤 7 记录）

1. **配置加载信号回授**：原 `_load_config_to_ui` 直接 setChecked/setValue 触发已连接的控制器回调 → 重复写盘、误日志、构造期间过早 show。修复：加载期间对相关控件 `blockSignals`，结束统一恢复。
2. **摄像头降级提示刷屏**：原每 tick 重置 `_presence_err_logged` → 不可用时每秒刷 5 条。修复：仅"启用边沿"（False→True）重置一次，不可用期间只提示一次。
3. **重置后全程关闭失效**：原 `reset_timer` 停掉全部扫描器且不再恢复。修复：改调 `_sync_guards_for_state()`，全程关闭保持"启用即运行"（旧版行为）。
4. **启动时初始调度缺失**：修复：`__init__` 末尾调用一次 `_sync_guards_for_state()`（配置启用"全程关闭"开机即生效）。
5. **进入休息强制清场语义偏差**：新 WorkGuard._scan 受开关与冷却限制，旧版 force=True 绕过。修复：`force_scan` → `_scan(force=True)`，force 时绕过开关与冷却。
6. **扫描间隔不实时生效**：原仅 start() 时读一次。修复：每次扫描后检查配置间隔，变化立即重设定时器。
7. **退出竞态**：on_quit 关闭统计库后心跳 tick 仍可能访问 → SQLite 异常。修复：先停 `_tick_timer`。
8. **跨线程定时器警告**：stop_all 直接在主线程调 worker 线程 QTimer.stop。修复：改用 `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`。
9. **细节**：主窗口未设图标；降级提示文案缺"摄像头检测不可用，已降级:"前缀；QFont/QRect/time 内联导入整理为顶层。

### 5.4 与设计草图的偏差（步骤 9 记录）

1. **托盘创建**：草图要求 main.py 创建 `TrayIcon(controller, window)`，实际 `MainWindow.__init__` 内部已自建托盘（`gui/main_window.py`），main.py 直接实例化 MainWindow 即可。
2. **单例锁位置**：`acquire_lock()` 在 `gui/main_window.py` 定义并经 `gui/__init__.py` 导出，main.py 直接导入使用。
3. **退出清理**：不挂 `app.aboutToQuit` → `guards.stop_all`/`presence.disable`（`controller.on_quit()` 中 `_log_work_segment(done=False)` 非幂等，重复调用重复写库）；退出清理仅由托盘「退出」/「以管理员身份重启」两条显式路径调用 `controller.on_quit()`。
4. **托盘常驻**：未设 `setQuitOnLastWindowClosed(False)`（旧版 main() 不设此标志；托盘不可用时 closeEvent accept 自动退出，保持与旧版一致）。

### 5.5 自动化验证记录

- `_step7_verify.py`：**36 项**全栈回归（组装 → 启动调度 → 计时/阶段切换 → 遮罩（短/长）→ 重置 → 列表管理 → force 清场 → 间隔同步 → 收起/展开 → 退出清理）；另以不可用检测器桩验证降级提示 3 秒内仅 1 条。
- `_step8_verify.py`：**53 项**（剥离组件独立性）：RestOverlay 短/长文本与几何、TrayIcon 菜单构建与 tooltip、format_duration 边界、BarChart 渲染与空数据、StatsPanel 四档刷新、theme 工具与尺寸常量、gui 包导出完整性、退出清理线程释放。
- `_step9_verify.py`：normal **13 项**（组装、初始调度、清理、配置/统计库隔离验证）＋ lock **3 项**（单例锁冲突返回 0、弹出提示、未创建窗口）。
- `_step11_verify.py`：**56 项**全栈回归（offscreen + PresenceStub 替代真实摄像头），验收条目（§5.7）全绿，exit 0。
- 所有验证脚本均把配置/统计库重定向到临时副本（如 `_step11_tmp`），不动真实数据。

### 5.6 风险提示与注意事项

1. **线程安全**：guards.py 扫描器运行在 QThread，`process_manager.kill()` 为阻塞操作，切勿在 GUI 线程直接调用；config.get() 只读并发可接受（配置变更仅主线程触发）。
2. **信号连接**：controller 与 main_window 的信号/槽使用默认 `Qt.AutoConnection`，跨线程信号自动排队，无需手动加锁。
3. **配置即时写盘**：`ConfigManager.set()` 内 save() 为同步 I/O，GUI 高频修改（如拖动滑块）可能短暂卡顿；可防抖，但现有设置项 <20 个预计无碍。
4. **旧配置文件兼容**：读取缺失字段用默认值填充并保存，老用户升级不丢数据。
5. **单例锁**：仍在 main.py 的 QLockFile，跨模块不共享锁文件逻辑。

### 5.7 验收标准（回归场景，全部通过）

- [x] 点击"开始" → 计时倒计时，进入工作阶段监管生效（若启用）。
- [x] 点击"暂停" → 倒计时暂停（手动暂停时摄像头检测随之停用、恢复计时时重新启用；仅"离开暂停"期间摄像头保持运行以支持自动恢复）。
- [x] 人离开摄像头超 15 秒 → 自动暂停；人回来 → 自动恢复。
- [x] 进入短休息/长休息 → 自动全屏遮罩，短休和长休内容不同。
- [x] 休息时人离开超 60 秒 → 遮罩关闭，本次休息不再重开。
- [x] "应用监管"列表中的进程在工作阶段被杀，休息阶段不杀。
- [x] "全程自动关闭"列表中的进程在包括休息、暂停在内的任何时间被杀。
- [x] "工作时段最小化"列表中的进程在工作运行时被最小化，暂停/休息时不触发。
- [x] 统计页"今日/本周/本月/累计"数字与柱状图显示正常。
- [x] 展开设置页修改任意项 → 配置即时保存，重启读取正确。
- [x] 托盘菜单"显示/隐藏"、"开始/暂停"、"跳过"、"重置"、"退出"全部正常。

---

## 6. 下一步待开发任务

- [ ] **（可选）提示音音量/音调可调**，或改用系统通知音
- [ ] **（可选）监管规则增强**：时间段规则（如工作日禁游戏）、累计时长上限、白名单
- [ ] **（可选）体验细节**：开机自启、全局快捷键、遮罩显示剩余休息时间、摄像头离开缓冲提示
- [ ] **（可选）单文件 onefile 或 Inno Setup 安装程序**分发（方案已调研，待定）
- [ ] **（明确不做）** 深浅主题切换

---

*快照为人工维护，功能/约束以源码与 README 为准；`pomodoro_guard_config.json` 为运行时产物，勿手动编辑。*
