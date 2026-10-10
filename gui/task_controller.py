"""任务生命周期管理；仅在线程退出且清理结果已产生后解锁界面。"""

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from .models import EventKind
from .simulation import SimulationBackend
from .real_backend import RealBackend
from .task_worker import TaskControl, TaskWorker


class TaskController(QObject):
    event = Signal(object)
    busy_changed = Signal(bool)
    task_finished = Signal(object)

    def __init__(self, parent=None, backend_factory=SimulationBackend, real_factory=RealBackend):
        super().__init__(parent)
        self.backend_factory = backend_factory
        self.real_factory = real_factory
        self.busy = False
        self.last_result = None
        self._thread = None
        self._worker = None
        self._control = None

    def start(self, parameters, settings, *, time_scale=0.15, scenario="normal", mode="simulation",
              real_confirmed=False):
        if self.busy:
            return False
        if mode not in ("simulation", "real"):
            raise ValueError("未知运行模式。")
        if mode == "real" and not real_confirmed:
            raise ValueError("真实任务必须先明确确认连接并启动。")
        parameters.validate(settings)
        self._control = TaskControl()
        self._thread = QThread(self)
        self._thread.setObjectName("SimulationTask" if mode == "simulation" else "RealDeviceTask")
        factory = self.backend_factory if mode == "simulation" else self.real_factory
        options = {"time_scale": time_scale, "scenario": scenario} if mode == "simulation" else {}
        self._worker = TaskWorker(parameters, settings, self._control, factory, options, mode)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self._on_event)
        # quit() 可跨线程调用；不依赖正在关闭的主窗口处理队列。
        self._worker.finished.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self.last_result = None
        self.busy = True
        self.busy_changed.emit(True)
        self._thread.start()
        return True

    @Slot(object)
    def _on_event(self, event):
        if event.kind == EventKind.RESULT:
            self.last_result = event
        self.event.emit(event)

    def cancel(self):
        if self.busy and self._control is not None:
            self._control.cancel()

    def answer_confirmation(self, request_id, accepted):
        if not self.busy or self._control is None:
            return False
        return self._control.answer(request_id, accepted)

    @Slot()
    def _on_thread_finished(self):
        thread = self._thread
        # finished 信号发出后，线程仍可能在析构线程局部资源。
        # 在主线程释放 Python 引用前确认原生线程真正退出；绝不阻塞 UI。
        if thread is not None and not thread.wait(0):
            QTimer.singleShot(1, self._on_thread_finished)
            return
        self._thread = self._worker = self._control = None
        self.busy = False
        if thread is not None:
            thread.deleteLater()
        self.busy_changed.emit(False)
        self.task_finished.emit(self.last_result)
