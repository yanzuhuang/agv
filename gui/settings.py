"""从已有入口读取默认值；QSettings 持久化不会连接设备。"""

from dataclasses import asdict, dataclass, fields
from pathlib import Path
import math
from inspect import signature

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Signal

import warehouse_agv as core_task
import warehouse_modbus as core_warehouse


_DEFAULTS = core_task.parse_args(["--operation", "exit"])
_PLC_DEFAULTS = signature(core_warehouse.WarehousePLC).parameters


@dataclass(frozen=True)
class DeviceSettings:
    plc_host: str = core_warehouse.PLC_HOST
    plc_port: int = core_warehouse.PLC_PORT
    device_id: int = core_warehouse.DEVICE_ID
    host: str = _DEFAULTS.host
    port: int = _DEFAULTS.port
    agv_id: int = _DEFAULTS.agv_id
    roller_profile: str | None = None
    material_input: int | None = None
    loaded_level: int | None = None
    query_data: int = _DEFAULTS.query_data
    timeout: float = _DEFAULTS.timeout
    plc_io_timeout: float = _PLC_DEFAULTS["io_timeout"].default
    plc_poll_interval: float = _PLC_DEFAULTS["poll_interval"].default
    ready_timeout: float = _DEFAULTS.ready_timeout
    start_timeout: float = _DEFAULTS.start_timeout
    action_timeout: float = _DEFAULTS.action_timeout
    travel_timeout: float = _DEFAULTS.travel_timeout
    transfer_timeout: float = _DEFAULTS.transfer_timeout
    poll_interval: float = _DEFAULTS.poll_interval

    def validate(self, require_profile=False):
        for name in ("plc_host", "host"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("PLC 和 AGV 地址不能为空。")
        for name, maximum in (("plc_port", 65535), ("port", 65535),
                              ("device_id", 247), ("agv_id", 255)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("设备端口或 ID 超出有效范围。")
        if self.roller_profile not in (None, "word", "pdf"):
            raise ValueError("滚筒定义仅支持 word 或 pdf。")
        if require_profile and self.roller_profile is None:
            raise ValueError("请先在系统设置中显式选择滚筒定义。")
        if (self.material_input is None) != (self.loaded_level is None):
            raise ValueError("物料输入 IO 与有料电平必须成对配置。")
        if self.material_input is not None:
            if type(self.material_input) is not int or not 1 <= self.material_input <= 8:
                raise ValueError("物料输入 IO 须为 1～8。")
            if type(self.loaded_level) is not int or self.loaded_level not in (0, 1):
                raise ValueError("有料电平须为 0 或 1。")
        if type(self.query_data) is not int or self.query_data not in (0, 1):
            raise ValueError("状态查询数据码须为 0 或 1。")
        for name in ("timeout", "plc_io_timeout", "plc_poll_interval", "ready_timeout", "start_timeout", "action_timeout",
                     "travel_timeout", "transfer_timeout", "poll_interval"):
            value = getattr(self, name)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise ValueError("所有超时与轮询间隔必须为有限正数。")


class SettingsStore:
    def __init__(self, filename=None):
        # 显式使用项目旁的 INI，便于 Windows 源码运行、移交及排查。
        filename = filename or Path(__file__).resolve().parents[1] / "gui_settings.ini"
        self.qsettings = QSettings(str(filename), QSettings.Format.IniFormat)
        self.load_error = ""

    @property
    def filename(self):
        return self.qsettings.fileName()

    def load(self):
        defaults = DeviceSettings()
        values = {}
        try:
            for entry in fields(defaults):
                default = getattr(defaults, entry.name)
                raw = self.qsettings.value(entry.name, default)
                if entry.name == "roller_profile":
                    value = raw or None
                elif entry.name in ("material_input", "loaded_level"):
                    value = None if raw in (None, "") else int(raw)
                elif entry.type is float:
                    value = float(raw)
                elif entry.type is int:
                    value = int(str(raw))
                else:
                    value = str(raw)
                values[entry.name] = value
            result = DeviceSettings(**values)
            result.validate()
            if self.qsettings.status() != QSettings.Status.NoError:
                raise ValueError("设置文件读取失败。")
            return result
        except (TypeError, ValueError) as exc:
            self.load_error = f"本地设置无效，已恢复代码默认值，滚筒定义需重新选择：{exc}"
            return defaults

    def save(self, settings):
        settings.validate()
        for name, value in asdict(settings).items():
            self.qsettings.setValue(name, "" if value is None else value)
        self.qsettings.sync()
        if self.qsettings.status() != QSettings.Status.NoError:
            raise OSError("设置保存失败，请检查 INI 文件写入权限。")


class SettingsDialog(QDialog):
    saved = Signal(object)

    def __init__(self, settings, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("系统设置")
        self.setMinimumWidth(470)
        self.store = store
        self.inputs = {}
        layout = QVBoxLayout(self)
        notice = QLabel("保存设置不连接设备；真实任务需在主窗口明确选择并确认启动。")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        for title, definitions in (
            ("仓库 PLC", (("plc_host", "IP 地址", "text"), ("plc_port", "端口", 65535),
                         ("device_id", "Slave ID", 247))),
            ("AGV", (("host", "IP 地址", "text"), ("port", "端口", 65535),
                     ("agv_id", "AGV ID", 255))),
        ):
            page = QWidget()
            form = QFormLayout(page)
            for name, label, kind in definitions:
                if kind == "text":
                    widget = QLineEdit(getattr(settings, name))
                else:
                    widget = QSpinBox()
                    widget.setRange(1, kind)
                    widget.setValue(getattr(settings, name))
                self.inputs[name] = widget
                form.addRow(label, widget)
            tabs.addTab(page, title)
        page = QWidget()
        form = QFormLayout(page)
        for name, label, choices in (
            ("roller_profile", "滚筒定义（需显式选择）", (
                ("请选择定义", None), ("word · 上料 04 / 下料 01", "word"),
                ("pdf · 上料 02 / 下料 03", "pdf"))),
            ("material_input", "物料输入 IO", (("不配置 · 使用人工确认", None),)
                + tuple((f"输入 IO {i}", i) for i in range(1, 9))),
            ("loaded_level", "有料电平", (("请选择电平", None), ("低电平 0", 0), ("高电平 1", 1))),
            ("query_data", "状态查询数据码", (("01", 1), ("00", 0))),
        ):
            widget = QComboBox()
            for text, value in choices:
                widget.addItem(text, value)
            widget.setCurrentIndex(max(0, widget.findData(getattr(settings, name))))
            self.inputs[name] = widget
            form.addRow(label, widget)
        for name, label in (
            ("timeout", "AGV 通信超时（秒）"), ("action_timeout", "动作超时（秒）"),
            ("plc_io_timeout", "PLC 通信超时（秒）"), ("plc_poll_interval", "PLC 轮询间隔（秒）"),
            ("travel_timeout", "行走超时（秒）"), ("poll_interval", "AGV 轮询间隔（秒）"),
            ("ready_timeout", "PLC 就绪超时（秒）"), ("start_timeout", "PLC 启动超时（秒）"),
            ("transfer_timeout", "人工确认超时（秒）"),
        ):
            widget = QDoubleSpinBox()
            widget.setDecimals(3)
            widget.setRange(0.001, 86400)
            widget.setValue(getattr(settings, name))
            self.inputs[name] = widget
            form.addRow(label, widget)
        tabs.addTab(page, "控制参数")
        self.inputs["material_input"].currentIndexChanged.connect(self._update_material)
        self._update_material()
        path_label = QLabel(f"设置文件：{store.filename}")
        path_label.setWordWrap(True)
        layout.addWidget(path_label)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox()
        buttons.addButton("保存设置", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_material(self):
        enabled = self.inputs["material_input"].currentData() is not None
        self.inputs["loaded_level"].setEnabled(enabled)
        if not enabled:
            self.inputs["loaded_level"].setCurrentIndex(0)

    def _save(self):
        values = {}
        for name, widget in self.inputs.items():
            if isinstance(widget, QLineEdit):
                values[name] = widget.text().strip()
            elif isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            else:
                values[name] = widget.value()
        try:
            result = DeviceSettings(**values)
            self.store.save(result)
        except (ValueError, OSError) as exc:
            self.error_label.setText(str(exc))
            return
        self.saved.emit(result)
        self.accept()
