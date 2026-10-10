"""后台执行器与主线程之间只交换事件及线程安全的控制信号。"""

from threading import Condition
import time
import queue

from PySide6.QtCore import QObject, Signal, Slot

from .models import EventKind, TaskEvent


class TaskCancelled(Exception):
    pass


class TaskControl:
    """不依赖 Worker 的 Qt 事件循环，阻塞确认期间也能立即取消。"""

    def __init__(self):
        self.condition = Condition()
        self.cancelled = False
        self.pending_id = None
        self.answer_value = None

    def cancel(self):
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()

    def checkpoint(self):
        with self.condition:
            if self.cancelled:
                raise TaskCancelled("用户请求取消任务。")

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        with self.condition:
            while True:
                if self.cancelled:
                    raise TaskCancelled("用户请求取消任务。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.condition.wait(remaining)

    def prepare_confirmation(self, request_id):
        with self.condition:
            if self.cancelled:
                raise TaskCancelled("用户请求取消任务。")
            self.pending_id = request_id
            self.answer_value = None

    def answer(self, request_id, accepted):
        with self.condition:
            if self.cancelled or self.pending_id != request_id or self.answer_value is not None:
                return False
            self.answer_value = bool(accepted)
            if not accepted:
                self.cancelled = True
            self.condition.notify_all()
            return True

    def wait_confirmation(self, request_id, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            try:
                while True:
                    if self.cancelled:
                        raise TaskCancelled("人工确认取消或用户请求停止。")
                    if self.pending_id != request_id:
                        raise RuntimeError("人工确认请求已失效。")
                    if self.answer_value is True:
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("等待模拟人工确认超时。")
                    self.condition.wait(remaining)
            finally:
                self.pending_id = None
                self.answer_value = None

    def poll_confirmation(self, request_id, timeout):
        """单次有界等待；未回答时让原调度继续检查真实 AGV 状态。"""
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                self.checkpoint()
                if self.pending_id != request_id:
                    raise RuntimeError("人工确认请求已失效。")
                if self.answer_value is True:
                    self.pending_id = self.answer_value = None
                    return ""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self.condition.wait(remaining)


class TaskWorker(QObject):
    event = Signal(object)
    finished = Signal()

    def __init__(self, parameters, settings, control, backend_factory, options, mode="simulation"):
        super().__init__()
        self.parameters = parameters
        self.settings = settings
        self.control = control
        self.backend_factory = backend_factory
        self.options = options
        self.current_stage = 0
        self.mode = mode

    def publish(self, kind, **data):
        if kind == EventKind.STAGE and data["status"] == "running":
            self.current_stage = data["stage"]
        self.event.emit(TaskEvent(kind, dict(data), mode=self.mode))

    @Slot()
    def run(self):
        outcome = "success"
        message = "模拟完整流程已完成；未连接或控制真实设备。"
        label = "模拟" if self.mode == "simulation" else "真实设备"
        if self.mode == "real":
            message = "完整流程已完成；请现场核对物料和滚筒停止状态。"
        cleanup_ok = False
        backend = None
        report = {}
        try:
            backend = self.backend_factory(self.parameters, self.settings, self.control,
                                           self.publish, **self.options)
            backend.run()
            self.control.checkpoint()
        except TaskCancelled as exc:
            outcome, message = "cancelled", str(exc)
            self.publish(EventKind.STAGE, stage=self.current_stage, status="cancelled", detail=message)
            self.publish(EventKind.LOG, level="WARNING", message=message)
        except Exception as exc:
            outcome, message = "failed", f"{label}任务失败：{exc}"
            self.publish(EventKind.STAGE, stage=self.current_stage, status="failed", detail=message)
            self.publish(EventKind.LOG, level="ERROR", message=message)
        finally:
            self.publish(EventKind.STAGE, stage=7, status="running", detail=f"正在清理{label}资源")
            try:
                if backend is not None:
                    report = backend.cleanup() or {}
                cleanup_ok = True
                cleanup_message = ("模拟资源已清理；没有真实设备连接。" if self.mode == "simulation"
                                   else "停止指令和连接清理已处理；滚筒机械停止需现场核对。")
                self.publish(EventKind.STAGE, stage=7, status="completed", detail=f"{label}资源已清理")
                self.publish(EventKind.CLEANUP, ok=True, message=cleanup_message, report=report)
            except Exception as exc:
                report = getattr(exc, "report", report)
                outcome, message = "failed", f"{label}清理失败：{exc}"
                self.publish(EventKind.STAGE, stage=7, status="failed", detail=message)
                self.publish(EventKind.CLEANUP, ok=False, message=message, report=report)
                self.publish(EventKind.LOG, level="ERROR", message=message)
            if outcome == "success" and self.control.cancelled:
                outcome, message = "cancelled", f"清理期间收到取消请求，{label}任务已结束。"
            self.publish(EventKind.LOG, level="INFO" if outcome == "success" else "WARNING", message=message)
            self.publish(EventKind.RESULT, outcome=outcome, message=message, cleanup_ok=cleanup_ok,
                         cleanup_report=report)
            self.finished.emit()
