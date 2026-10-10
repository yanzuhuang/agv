"""中文单窗口控制台，所有 QWidget 操作均位于主线程。"""

from datetime import datetime
from html import escape

from PySide6.QtCore import QRectF, QTimer, Qt, Slot
from PySide6.QtGui import QColor, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QMainWindow, QPushButton, QRadioButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

import outbound_agv as core_agv

from .models import EventKind, STAGES, TaskParameters
from .settings import SettingsDialog, SettingsStore
from .simulation import SCENARIOS
from .storage_grid import StorageGrid, refresh_style
from .task_controller import TaskController


STATUS_TEXT = {
    "waiting": "等待中", "running": "执行中", "completed": "已完成",
    "failed": "失败", "cancelled": "已取消",
}


class AgvIcon(QWidget):
    """固定的车辆符号，仅表达状态，不表达连续地图位置。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(54, 36)
        self.moving = False
        self.setToolTip("AGV 状态示意，不表示精确位置")

    def set_moving(self, moving):
        self.moving = moving
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2563cb" if self.moving else "#7188a5"))
        painter.drawRoundedRect(QRectF(9, 5, 36, 23), 5, 5)
        painter.setBrush(QColor("#283d58"))
        for x in (7, 44):
            for y in (8, 21):
                painter.drawRoundedRect(QRectF(x, y, 4, 7), 1, 1)
        painter.setPen(QColor("white"))
        painter.drawText(QRectF(9, 5, 36, 23), Qt.AlignmentFlag.AlignCenter, "AGV")


def card(title):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 12, 16, 12)
    label = QLabel(title)
    label.setObjectName("cardTitle")
    layout.addWidget(label)
    return frame, layout


def scrollable(widget):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(widget)
    return scroll


class MainWindow(QMainWindow):
    def __init__(self, store=None, controller=None, time_scale=0.15):
        super().__init__()
        self.setWindowTitle("仓储 AGV 联动控制系统 · 模拟模式")
        self.setMinimumSize(960, 580)
        self.store = store or SettingsStore()
        self.settings = self.store.load()
        self.controller = controller or TaskController(self)
        self.time_scale = time_scale
        self.stage_states = ["waiting"] * len(STAGES)
        self.device_values = {}
        self.confirmation_dialog = None
        self.settings_dialog = None
        self.real_start_dialog = None
        self.active_mode = "simulation"
        self._closing = False
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("仓储 AGV 联动控制系统")
        title.setObjectName("mainTitle")
        header.addWidget(title)
        header.addStretch()
        self.plc_badge = QLabel("仓库 PLC · 模拟模式")
        self.agv_badge = QLabel("AGV · 模拟模式")
        self.task_badge = QLabel("任务 · 待机（模拟）")
        for badge in (self.plc_badge, self.agv_badge, self.task_badge):
            badge.setObjectName("badge")
            header.addWidget(badge)
        self.top_settings_button = QPushButton("系统设置")
        self.top_settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.top_settings_button)
        root.addLayout(header)
        self.banner = QLabel("模拟模式  ·  不连接或控制真实 PLC / AGV  ·  演示时间已缩短，确认仍需人工操作")
        self.banner.setObjectName("simulationBanner")
        self.banner.setWordWrap(True)
        root.addWidget(self.banner)
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(self.vertical_splitter, 1)
        body = QSplitter(Qt.Orientation.Horizontal)
        self.parameter_panel = self._build_parameters()
        left_scroll = scrollable(self.parameter_panel)
        left_scroll.setMinimumWidth(365)
        body.addWidget(left_scroll)
        monitor = QWidget()
        monitor_layout = QVBoxLayout(monitor)
        monitor_layout.setContentsMargins(0, 0, 0, 0)
        monitor_layout.setSpacing(10)
        monitor_layout.addWidget(self._build_stages())
        self.agv_panel = self._build_agv()
        monitor_layout.addWidget(self.agv_panel, 1)
        body.addWidget(scrollable(monitor))
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([390, 830])
        self.vertical_splitter.addWidget(body)
        self.vertical_splitter.addWidget(self._build_logs())
        self.vertical_splitter.setStretchFactor(0, 4)
        self.vertical_splitter.setStretchFactor(1, 1)
        self.vertical_splitter.setSizes([520, 130])
        footer = QHBoxLayout()
        note = QLabel("请求停止将取消任务并清理连接；机械急停请使用设备上的急停按钮。")
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("模拟模式", "simulation")
        self.mode_combo.addItem("真实设备模式", "real")
        footer.addWidget(self.mode_combo)
        self.settings_button = QPushButton("系统设置")
        self.settings_button.clicked.connect(self.open_settings)
        self.stop_button = QPushButton("请求停止")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.request_stop)
        self.start_button = QPushButton("开始模拟任务")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_task)
        for button in (self.settings_button, self.stop_button, self.start_button):
            footer.addWidget(button)
        root.addLayout(footer)
        self.controller.event.connect(self.handle_event)
        self.controller.busy_changed.connect(self._set_busy)
        self.controller.task_finished.connect(self._task_finished)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self._append_log("INFO", "模拟模式已就绪；真实设备需明确选择模式并确认启动后才连接。")
        if self.store.load_error:
            self._append_log("WARNING", self.store.load_error)
        screen = self.screen().availableGeometry()
        self.setMinimumSize(min(960, screen.width()), min(580, screen.height()))
        self.resize(min(screen.width(), max(960, min(1280, int(screen.width() * 0.93)))),
                    min(screen.height(), max(580, min(850, int(screen.height() * 0.90)))))

    def _build_parameters(self):
        panel, layout = card("任务参数 · A 库")
        layout.setSpacing(6)
        fields = QGridLayout()
        fields.setSpacing(6)
        for index, prefix, title in ((0, "outbound", "出库源库位"), (1, "inbound", "入库目标库位")):
            fields.addWidget(QLabel(title), index, 0)
            column, level = QSpinBox(), QSpinBox()
            column.setRange(1, 6)
            column.setValue(6)
            level.setRange(1, 3)
            column.setSuffix(" 列")
            level.setSuffix(" 层")
            setattr(self, f"{prefix}_column", column)
            setattr(self, f"{prefix}_level", level)
            fields.addWidget(column, index, 1)
            fields.addWidget(level, index, 2)
            column.valueChanged.connect(self._sync_grid)
            level.valueChanged.connect(self._sync_grid)
        layout.addLayout(fields)
        duration_row = QHBoxLayout()
        duration_row.addWidget(QLabel("线体时间 T"))
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setDecimals(2)
        self.duration_input.setRange(0.01, 86400)
        self.duration_input.setSingleStep(0.5)
        self.duration_input.setSuffix(" 秒")
        self.duration_input.setValue(core_agv.LINE_DURATION)
        duration_row.addWidget(self.duration_input, 1)
        layout.addLayout(duration_row)
        timing = QLabel("T > 2 秒且不超过动作超时。\n上料重叠最后 2 秒，下料重叠最初 2 秒。")
        timing.setObjectName("mutedLabel")
        timing.setWordWrap(True)
        layout.addWidget(timing)
        modes = QHBoxLayout()
        self.outbound_radio = QRadioButton("选择出库位置")
        self.inbound_radio = QRadioButton("选择入库位置")
        self.outbound_radio.setChecked(True)
        self.selection_group = QButtonGroup(self)
        for radio in (self.outbound_radio, self.inbound_radio):
            self.selection_group.addButton(radio)
            modes.addWidget(radio)
        layout.addLayout(modes)
        self.storage_grid = StorageGrid()
        self.storage_grid.selected.connect(self._select_slot)
        self.outbound_radio.toggled.connect(lambda checked: checked and self.storage_grid.set_mode("outbound"))
        self.inbound_radio.toggled.connect(lambda checked: checked and self.storage_grid.set_mode("inbound"))
        layout.addWidget(self.storage_grid)
        legend = QLabel("蓝色：出库  ·  绿色：入库  ·  双色：同一库位\n仅表示本次任务选择，不代表真实库存。")
        legend.setObjectName("mutedLabel")
        legend.setWordWrap(True)
        layout.addWidget(legend)
        scenario_row = QHBoxLayout()
        scenario_row.addWidget(QLabel("模拟场景"))
        self.scenario_combo = QComboBox()
        for title, scenario in SCENARIOS:
            self.scenario_combo.addItem(title, scenario)
        scenario_row.addWidget(self.scenario_combo, 1)
        layout.addLayout(scenario_row)
        self.validation_label = QLabel()
        self.validation_label.setObjectName("errorLabel")
        self.validation_label.setWordWrap(True)
        self.validation_label.hide()
        layout.addWidget(self.validation_label)
        layout.addStretch()
        self._sync_grid()
        return panel

    def _build_stages(self):
        panel, layout = card("完整流程监控")
        layout.setSpacing(4)
        self.plc_feedback = QLabel("PLC 反馈：尚无模拟反馈")
        self.plc_feedback.setObjectName("mutedLabel")
        layout.addWidget(self.plc_feedback)
        self.stage_rows = []
        for index, title in enumerate(STAGES):
            row = QFrame()
            row.setProperty("stageState", "waiting")
            row.setObjectName("stageRow")
            line = QHBoxLayout(row)
            line.setContentsMargins(10, 3, 10, 3)
            number = QLabel(f"{index + 1:02d}")
            number.setObjectName("stageNumber")
            line.addWidget(number)
            name = QLabel(title)
            name.setMinimumWidth(150)
            line.addWidget(name)
            detail = QLabel("等待任务事件")
            detail.setObjectName("stageDetail")
            detail.setWordWrap(True)
            line.addWidget(detail, 1)
            state = QLabel("等待中")
            state.setObjectName("stageStatus")
            state.setMinimumWidth(48)
            line.addWidget(state)
            layout.addWidget(row)
            self.stage_rows.append((row, detail, state))
        return panel

    def _build_agv(self):
        panel, layout = card("AGV 状态 · 离散站点示意（模拟）")
        self.agv_title = layout.itemAt(0).widget()
        route = QHBoxLayout()
        self.station1_label = QLabel("站点 1\n上下料交接")
        self.station2_label = QLabel("站点 2\n往返折返点")
        self.route_label = QLabel("←  AGV 等待模拟反馈  →")
        self.route_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.route_label.setWordWrap(True)
        self.route_label.setMinimumWidth(260)
        center = QWidget()
        center_layout = QHBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addStretch()
        self.agv_icon = AgvIcon()
        center_layout.addWidget(self.agv_icon)
        center_layout.addWidget(self.route_label)
        center_layout.addStretch()
        for label in (self.station1_label, self.station2_label):
            label.setObjectName("stationLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setProperty("confirmed", False)
        route.addWidget(self.station1_label)
        route.addWidget(center, 1)
        route.addWidget(self.station2_label)
        layout.addLayout(route)
        grid = QGridLayout()
        self.agv_labels = {}
        for index, (key, title) in enumerate((
            ("confirmed_station", "确认站点"), ("target_station", "目标站点"),
            ("state", "运行状态"), ("speed_percent", "速度比例"),
            ("alarm", "报警信息"), ("voltage", "电压信息"),
            ("raw_station", "原始站点"),
        )):
            row, column = divmod(index, 2)
            label = QLabel("—")
            label.setWordWrap(True)
            self.agv_labels[key] = label
            name = QLabel(title)
            name.setObjectName("mutedLabel")
            grid.addWidget(name, row, column * 2)
            grid.addWidget(label, row, column * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)
        layout.addStretch()
        return panel

    def _build_logs(self):
        panel = QFrame()
        panel.setObjectName("card")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        header = QHBoxLayout()
        title = QLabel("运行日志 · 模拟事件")
        self.logs_title = title
        title.setObjectName("cardTitle")
        header.addWidget(title)
        header.addStretch()
        self.clear_logs_button = QPushButton("清空显示")
        header.addWidget(self.clear_logs_button)
        layout.addLayout(header)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(65)
        self.clear_logs_button.clicked.connect(self.log_view.clear)
        layout.addWidget(self.log_view)
        return panel

    def task_parameters(self):
        return TaskParameters(self.outbound_column.value(), self.outbound_level.value(),
                              self.inbound_column.value(), self.inbound_level.value(),
                              self.duration_input.value())

    def _sync_grid(self):
        if hasattr(self, "storage_grid"):
            self.storage_grid.set_selection(
                (self.outbound_column.value(), self.outbound_level.value()),
                (self.inbound_column.value(), self.inbound_level.value()))

    def _select_slot(self, mode, column, level):
        getattr(self, f"{mode}_column").setValue(column)
        getattr(self, f"{mode}_level").setValue(level)

    @Slot()
    def start_task(self):
        if self.controller.busy or self._closing or self.real_start_dialog is not None:
            return
        try:
            parameters = self.task_parameters()
            parameters.validate(self.settings)
        except ValueError as exc:
            self.validation_label.setText(str(exc))
            self.validation_label.show()
            self._append_log("WARNING", str(exc))
            return
        self.validation_label.clear()
        self.validation_label.hide()
        if self.mode_combo.currentData() == "real":
            self._confirm_real_start(parameters)
            return
        self._begin_task(parameters, self.settings, "simulation")

    def _confirm_real_start(self, parameters):
        settings = self.settings
        dialog = QDialog(self)
        self.real_start_dialog = dialog
        dialog.setWindowTitle("真实任务 · 核对参数并启动")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        text = QLabel(f"将连接并控制以下设备：\n"
                      f"PLC  {settings.plc_host}:{settings.plc_port} · Slave {settings.device_id}\n"
                      f"AGV  {settings.host}:{settings.port} · ID {settings.agv_id}\n\n"
                      f"A 库 {parameters.column}-{parameters.level} 出库 → AGV 往返 → "
                      f"{parameters.inbound_column}-{parameters.inbound_level} 入库\n"
                      f"线体 T={parameters.duration:g} 秒 · 滚筒定义 {settings.roller_profile}\n"
                      f"上料码 0x{core_agv.ROLLER_PROFILES[settings.roller_profile][0]:02X} · "
                      f"下料码 0x{core_agv.ROLLER_PROFILES[settings.roller_profile][1]:02X}\n"
                      f"物料 IO：{settings.material_input if settings.material_input is not None else '未配置，上料后人工确认'}\n\n"
                      "连接后会发送初始化停止指令，并再次要求确认现场准备条件。\n"
                      "确认设备配置及现场条件符合要求后启动。")
        text.setWordWrap(True)
        layout.addWidget(text)
        buttons = QDialogButtonBox()
        self.real_start_button = buttons.addButton("确认连接并启动任务", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        def finished(result):
            self.real_start_dialog = None
            dialog.deleteLater()
            self._set_busy(False)
            if result == QDialog.DialogCode.Accepted and not self._closing:
                self._begin_task(parameters, settings, "real", real_confirmed=True)
        dialog.finished.connect(finished)
        self._set_busy(False)
        dialog.open()

    def _begin_task(self, parameters, settings, mode, real_confirmed=False):
        self.active_mode = mode
        self.device_values.clear()
        self._update_device({})
        for index in range(len(STAGES)):
            self._update_stage(index, "waiting", "等待任务事件")
        self.task_badge.setText(f"任务 · {self.mode_label}运行中")
        self.task_badge.setProperty("result", "")
        refresh_style(self.task_badge)
        self._append_log("INFO", f"开始{self.mode_label}任务：{parameters.column}-{parameters.level} 出库 → "
                         f"AGV 往返 → {parameters.inbound_column}-{parameters.inbound_level} 入库；T={parameters.duration:g} 秒。")
        self.controller.start(parameters, settings, time_scale=self.time_scale,
                              scenario=self.scenario_combo.currentData(), mode=mode,
                              real_confirmed=real_confirmed)

    @property
    def mode_label(self):
        return "模拟" if self.active_mode == "simulation" else "真实"

    @Slot()
    def _mode_changed(self):
        self.active_mode = self.mode_combo.currentData()
        self.setWindowTitle(f"仓储 AGV 联动控制系统 · {self.mode_label}模式")
        self.banner.setText("模拟模式  ·  不连接或控制真实 PLC / AGV  ·  演示时间已缩短，确认仍需人工操作"
                            if self.active_mode == "simulation" else
                            "真实设备模式  ·  启动前需明确确认设备参数  ·  装卸使用实际秒数  ·  请在现场核对物料状态")
        self.start_button.setText(f"开始{self.mode_label}任务")
        self.scenario_combo.setEnabled(self.active_mode == "simulation")
        self.agv_title.setText(f"AGV 状态 · 离散站点示意（{self.mode_label}）")
        self.logs_title.setText(f"运行日志 · {self.mode_label}事件")
        self.device_values.clear()
        self._update_device({})
        for index in range(len(STAGES)):
            self._update_stage(index, "waiting", "等待任务事件")
        self.task_badge.setText(f"任务 · 待机（{self.mode_label}）")
        self.task_badge.setProperty("result", "")
        refresh_style(self.task_badge)

    @Slot(bool)
    def _set_busy(self, busy):
        locked = busy or self.real_start_dialog is not None
        self.parameter_panel.setEnabled(not locked)
        self.start_button.setEnabled(not locked and not self._closing)
        self.scenario_combo.setEnabled(not locked and self.active_mode == "simulation")
        self.stop_button.setEnabled(busy and not self._closing)
        for widget in (self.settings_button, self.top_settings_button, self.mode_combo):
            widget.setEnabled(not locked and not self._closing)

    @Slot()
    def request_stop(self):
        if not self.controller.busy:
            return
        self.controller.cancel()
        self.stop_button.setEnabled(False)
        self.task_badge.setText(f"任务 · 正在取消与清理（{self.mode_label}）")
        self._dismiss_confirmation()
        self._append_log("WARNING", f"已请求停止{self.mode_label}任务，等待后台停止指令与清理结果。")

    @Slot(object)
    def handle_event(self, event):
        if event.kind == EventKind.STAGE:
            self._update_stage(event.data["stage"], event.data["status"], event.data["detail"])
        elif event.kind == EventKind.DEVICE:
            self._update_device(event.data)
        elif event.kind == EventKind.LOG:
            self._append_log(event.data["level"], event.data["message"], event.timestamp)
        elif event.kind == EventKind.CONFIRMATION:
            if not self._closing and self.stop_button.isEnabled():
                self._show_confirmation(event.data)
        elif event.kind == EventKind.RESULT:
            self._dismiss_confirmation()
            text = self.mode_label + {"success": "完成", "cancelled": "已取消", "failed": "失败"}[event.data["outcome"]]
            self.task_badge.setText(f"任务 · {text}")
            self.task_badge.setProperty("result", event.data["outcome"])
            refresh_style(self.task_badge)
        elif event.kind == EventKind.CLEANUP:
            self._append_log("INFO" if event.data["ok"] else "ERROR", event.data["message"])

    def _update_stage(self, index, status, detail):
        self.stage_states[index] = status
        row, detail_label, state = self.stage_rows[index]
        row.setProperty("stageState", status)
        detail_label.setText(detail)
        state.setText(STATUS_TEXT[status])
        refresh_style(row)

    def _update_device(self, values):
        self.device_values.update(values)
        data = self.device_values
        self.plc_badge.setText("仓库 PLC · " + ("模拟模式" if self.active_mode == "simulation" else data.get("plc_connection", "未连接")))
        self.agv_badge.setText("AGV · " + ("模拟模式" if self.active_mode == "simulation" else data.get("agv_connection", "未连接")))
        ready = data.get("plc_ready")
        ready_text = "是" if ready is True else "否" if ready is False else "—"
        if data.get("plc_ready_raw", 1) not in (0, 1):
            ready_text = f"异常值 {data['plc_ready_raw']}"
        self.plc_feedback.setText(f"PLC 反馈（{self.mode_label}）：{data.get('plc_state', '—')}  ·  就绪：{ready_text}")
        confirmed, target = data.get("confirmed_station"), data.get("target_station")
        transit = data.get("in_transit", False)
        self.agv_icon.set_moving(transit)
        for key, label in self.agv_labels.items():
            value = data.get(key)
            if key in ("confirmed_station", "target_station"):
                text = f"站点 {value}" if value is not None else "—"
                if key == "confirmed_station" and value is not None and transit:
                    text += "（上次确认）"
            elif key == "speed_percent":
                text = f"{value}%" if value is not None else "—"
            else:
                text = str(value) if value is not None else "—"
            label.setText(text)
        if transit:
            direction = "前往站点 2" if target == 2 else "返回站点 1" if target == 1 else "移动 / 状态待核对"
            self.route_label.setText(f"AGV · {direction}\n无连续位置坐标（{self.mode_label}）")
        elif confirmed is not None:
            suffix = "\n最后一次任务反馈" if data.get("agv_connection") == "已断开" else ""
            self.route_label.setText(f"AGV · 站点 {confirmed} 已确认（{self.mode_label}）{suffix}")
        else:
            self.route_label.setText(f"←  AGV 等待{self.mode_label}反馈  →")
        for number, label in ((1, self.station1_label), (2, self.station2_label)):
            label.setProperty("confirmed", not transit and confirmed == number)
            refresh_style(label)

    def _append_log(self, level, message, timestamp=None):
        color = {"INFO": "#40536b", "WARNING": "#a15b00", "ERROR": "#c63542"}.get(level, "#40536b")
        stamp = (timestamp or datetime.now()).strftime("%H:%M:%S")
        self.log_view.append(f'<span style="color:{color}">[{stamp}] [{escape(level)}] {escape(message)}</span>')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _show_confirmation(self, request):
        self._dismiss_confirmation()
        dialog = QDialog(self)
        self.confirmation_dialog = dialog
        dialog.setWindowTitle(f"{self.mode_label}确认 · {request['title']}")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumWidth(430)
        layout = QVBoxLayout(dialog)
        title = QLabel(request["title"])
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        body = QLabel(request["message"])
        body.setWordWrap(True)
        layout.addWidget(body)
        layout.addWidget(QLabel(f"等待上限：{request['timeout']:g} 秒；取消将终止本次{self.mode_label}任务。"))
        buttons = QDialogButtonBox()
        self.confirm_button = buttons.addButton(f"确认并继续{self.mode_label}", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消任务", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.finished.connect(lambda result, rid=request["request_id"]:
                                self.controller.answer_confirmation(rid, result == QDialog.DialogCode.Accepted))
        dialog.finished.connect(lambda result, d=dialog: self._confirmation_closed(d))
        self.task_badge.setText(f"任务 · 等待人工确认（{self.mode_label}）")
        dialog.open()

    def _confirmation_closed(self, dialog):
        if self.confirmation_dialog is dialog:
            self.confirmation_dialog = None
        dialog.deleteLater()
        if self.controller.busy and self.stop_button.isEnabled():
            self.task_badge.setText(f"任务 · {self.mode_label}运行中")

    def _dismiss_confirmation(self):
        dialog = self.confirmation_dialog
        if dialog is not None:
            dialog.reject()

    @Slot()
    def open_settings(self):
        if self.controller.busy or self._closing:
            return
        if self.settings_dialog is not None:
            self.settings_dialog.raise_()
            return
        dialog = SettingsDialog(self.settings, self.store, self)
        self.settings_dialog = dialog
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.saved.connect(self._settings_saved)
        dialog.finished.connect(self._settings_closed)
        dialog.open()

    def _settings_saved(self, settings):
        self.settings = settings
        self.validation_label.clear()
        self.validation_label.hide()
        self._append_log("INFO", "系统设置已保存；保存操作不连接设备。")

    def _settings_closed(self, result):
        dialog, self.settings_dialog = self.settings_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    @Slot(object)
    def _task_finished(self, result):
        self._dismiss_confirmation()
        if self._closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.controller.busy:
            self._closing = True
            self.request_stop()
            self._set_busy(True)
            event.ignore()
        else:
            if self.real_start_dialog is not None:
                self._closing = True
                self.real_start_dialog.reject()
            self._dismiss_confirmation()
            event.accept()
