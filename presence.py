# -*- coding: utf-8 -*-
"""
摄像头人像在场检测 —— 核心逻辑层（UI 无关）
============================================
用「人脸 + 人体」组合检测判断"人是否在摄像头前"：

- YuNet 人脸检测：正对/侧对镜头的人脸；
- YOLOv8n 人体检测（COCO person 类）：低头写作业、背对镜头时人脸不可见，
  但人形仍在画面中 —— 人脸或人体**任一命中即视为"人在"**，只有两者都
  检测不到（人离开座位）才算"不在"；
- 独立后台线程，按固定间隔（默认 5 秒）采样一帧，低分辨率 640x480；
- 记录"最近一次检测到人"的 monotonic 时间戳，供上层判定"连续不在 N 秒"；
- 摄像头不可用 / 被占用 / 权限被拒 / 模型缺失时自动降级为"检测不可用"，
  只记录原因，绝不抛出异常；人脸/人体两个模型相互独立，缺失其一则只用另一个。

上层（界面层）职责：按阶段启用/停用检测，并应用业务规则
（专注离开暂停/回来自动恢复、休息无人关闭遮罩等）。
"""

import os
import sys
import threading
import time

try:
    import cv2
    HAS_CV2 = True
except ImportError:  # 未安装 opencv 时整体降级
    cv2 = None
    HAS_CV2 = False


def _locate_model(name):
    """模型文件查找顺序：exe/脚本同目录 → PyInstaller onedir 的 _internal。"""
    if getattr(sys, "frozen", False):
        cands = [
            os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name),
            os.path.join(getattr(sys, "_MEIPASS", "") or "", name),
        ]
        return next((p for p in cands if p and os.path.exists(p)), cands[0])
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


MODEL_PATH = _locate_model("face_detection_yunet_2023mar.onnx")   # 人脸
PERSON_MODEL_PATH = _locate_model("person_det.onnx")              # 人体(YOLOv8n)

DETECT_INTERVAL = 5.0   # 采样间隔（秒）
FRAME_WIDTH = 640       # 采样分辨率（"适当提升"：640x480，兼顾准确度与负载）
FRAME_HEIGHT = 480
SCORE_THRESHOLD = 0.6   # YuNet 人脸置信度阈值
PERSON_SCORE_THRESHOLD = 0.5  # YOLOv8n person 类置信度阈值
PERSON_INPUT_SIZE = 640       # YOLOv8n 输入边长


class PresenceDetector:
    """摄像头在场检测器。

    线程模型：主线程调用 enable()/disable()/stop() 控制启停，
    后台线程循环采样并维护只读状态；主线程通过 is_present()/last_seen_age()
    等查询方法（线程安全）读取结果。
    """

    def __init__(self, interval=DETECT_INTERVAL, model_path=MODEL_PATH,
                 person_model_path=PERSON_MODEL_PATH):
        self.interval = max(0.5, float(interval))
        self.model_path = model_path
        self.person_model_path = person_model_path
        self._thread = None
        self._stop = threading.Event()      # 本次启停的停止信号
        self._lock = threading.Lock()
        self._cap = None                    # cv2.VideoCapture
        self._detector = None               # cv2.FaceDetectorYN（人脸）
        self._person_net = None             # cv2.dnn.Net（人体，YOLOv8n）
        self._last_seen = 0.0               # monotonic：最近一次检测到人的时刻
        self._present = False               # 最近一次采样是否有人
        self._available = False             # 检测器是否正在工作
        self._error = ""                    # 不可用原因
        self._samples = 0                   # 已采样次数（测试/日志用）

    # ---------------------------- 状态查询（线程安全） ----------------------------
    def is_available(self):
        """检测器是否正在工作（摄像头+模型均可用）。"""
        with self._lock:
            return self._available

    def is_present(self):
        """最近一次采样是否检测到人脸。"""
        with self._lock:
            return self._present

    def last_seen_age(self):
        """距最近一次看到人脸已过去多少秒（float）。

        从未看到人脸时返回自启用起的时长（由 _last_seen 初始化保证），
        避免"刚启用还没采样"被误判为长期不在。
        """
        with self._lock:
            if self._last_seen <= 0:
                return float("inf")
            return max(0.0, time.monotonic() - self._last_seen)

    def error(self):
        with self._lock:
            return self._error

    # ---------------------------- 控制（主线程调用） ----------------------------
    def enable(self):
        """启用检测（幂等）。摄像头/模型就绪在后台线程完成。"""
        if not HAS_CV2:
            with self._lock:
                if not self._error:
                    self._error = "未安装 opencv，摄像头检测不可用"
            return
        with self._lock:
            if self._available:
                return  # 已在工作
        if self._thread is not None and self._thread.is_alive():
            return  # 线程仍在（正在就绪/重试中）
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="presence-detect")
        self._thread.start()

    def disable(self):
        """停用检测并释放摄像头（幂等）。"""
        with self._lock:
            if self._available:
                self._available = False
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._present = False
            self._last_seen = 0.0

    def stop(self):
        """程序退出时调用：彻底停止（之后 enable 不会重启）。"""
        self.disable()

    # ---------------------------- 内部实现 ----------------------------
    def _acquire(self):
        """尝试打开摄像头并加载人脸/人体模型；至少一个检测器可用即成功。

        摄像头不可用 → 整体不可用；人脸/人体模型相互独立，缺失其一则只用另一个。
        """
        with self._lock:
            self._error = ""
        # 摄像头（两个检测器的公共依赖）
        if self._cap is None or not self._cap.isOpened():
            try:
                cap = cv2.VideoCapture(0)  # 第一个可用摄像头
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
                if not cap.isOpened():
                    cap.release()
                    with self._lock:
                        self._error = "摄像头不可用或被占用"
                    return False
                self._cap = cap
            except Exception as exc:
                with self._lock:
                    self._error = f"打开摄像头失败: {exc}"
                self._cap = None
                return False
        # 人脸模型（可选）
        if self._detector is None and os.path.exists(self.model_path):
            try:
                self._detector = cv2.FaceDetectorYN.create(
                    self.model_path, "",
                    (FRAME_WIDTH, FRAME_HEIGHT), SCORE_THRESHOLD)
            except Exception as exc:
                with self._lock:
                    self._error = f"人脸模型加载失败: {exc}"
                self._detector = None
        # 人体模型（可选）
        if self._person_net is None and os.path.exists(self.person_model_path):
            try:
                self._person_net = cv2.dnn.readNetFromONNX(self.person_model_path)
            except Exception as exc:
                with self._lock:
                    self._error = f"人体检测模型加载失败: {exc}"
                self._person_net = None
        # 至少一个检测器可用才算成功
        if self._detector is None and self._person_net is None:
            with self._lock:
                missing = [n for n, p in
                           ((os.path.basename(self.model_path), self.model_path),
                            (os.path.basename(self.person_model_path),
                             self.person_model_path))
                           if not os.path.exists(p)]
                base = "人脸与人体检测模型均不可用" + (
                    f"（缺失: {', '.join(missing)}）" if missing else "（加载失败）")
                self._error = base
            return False
        return True

    def _release(self):
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _run(self):
        """后台主循环：就绪后每 interval 秒采样一帧并检测。"""
        if not self._acquire():
            with self._lock:
                self._available = False
            return  # 降级：线程退出，等下次 enable 再试
        with self._lock:
            self._available = True
            # 从未检测到人时从"启用时刻"起算，避免启动即误判不在
            self._last_seen = time.monotonic()
            self._present = False
        try:
            next_at = time.monotonic()
            while not self._stop.is_set():
                next_at += self.interval
                self._sample_once()
                # 睡眠到下一个采样点（单调时钟对齐，防漂移）
                delay = next_at - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
        finally:
            self._release()
            with self._lock:
                self._available = False
                self._present = False

    def _sample_once(self):
        """读一帧并检测人脸，更新 present / last_seen。"""
        cap = self._cap
        if cap is None:
            return
        try:
            ok, frame = cap.read()
            if not ok or frame is None:
                with self._lock:
                    if not self._error:
                        self._error = "读取摄像头画面失败（可能已被拔出）"
                return
        except Exception as exc:
            with self._lock:
                if not self._error:
                    self._error = f"读取摄像头画面失败: {exc}"
            return
        # 组合检测：人脸 或 人体任一命中 = "人在"
        try:
            present = False
            # 1) YuNet 人脸检测
            if self._detector is not None:
                _, faces = self._detector.detect(frame)
                if faces is not None and len(faces) > 0:
                    present = True
            # 2) YOLOv8n 人体检测（人脸不可见时兜底：低头/背对镜头）
            if not present and self._person_net is not None:
                blob = cv2.dnn.blobFromImage(
                    frame, 1.0 / 255.0, (PERSON_INPUT_SIZE, PERSON_INPUT_SIZE),
                    swapRB=True, crop=False)
                self._person_net.setInput(blob)
                out = self._person_net.forward()  # (1, 84, 8400)
                # YOLOv8 输出：行 0-3=框，行 4=person 类（COCO 第 0 类）分数
                if out is not None and out.ndim == 3 and out.shape[1] >= 5:
                    if float(out[0, 4, :].max()) >= PERSON_SCORE_THRESHOLD:
                        present = True
            now = time.monotonic()
            with self._lock:
                self._samples += 1
                if present:
                    self._present = True
                    self._last_seen = now
                    self._error = ""
                else:
                    self._present = False
        except Exception as exc:
            with self._lock:
                if not self._error:
                    self._error = f"人像检测失败: {exc}"
