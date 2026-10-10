"""立体仓库 PLC 控制接口，使用 pymodbus 3.11.4。

地址以文档寄存器总表及标注截图为准：40005（协议地址 4）为命令。
每个动作必须观察到忙碌 -> 空闲才算完成，命令写入不自动重试。
"""

import math
import time
from datetime import datetime
from contextvars import ContextVar

from pymodbus.client import ModbusTcpClient


STATUS = 0             # 40001，只读：0 空闲，1 忙碌，2 未回零
COLUMN = 1             # 40002，只写：1～6
LEVEL = 2              # 40003，只写：1～3
WAREHOUSE = 3          # 40004，只写：1 为 A 库
COMMAND = 4            # 40005，只写：0 无，1 出库，2 开出，3 开进，4 入库
READY = 5              # 40006，只读：0 未就绪，1 已就绪


LOG_SINK = ContextVar("warehouse_log_sink", default=None)


def log(message):
    sink = LOG_SINK.get()
    if sink is not None:
        try:
            sink(message)
            return
        except Exception:
            pass
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


class WarehouseError(RuntimeError):
    pass


class WarehousePLC:
    def __init__(self, host="192.168.1.11", port=502, device_id=1,
                 io_timeout=3, ready_timeout=30, start_timeout=15,
                 action_timeout=180, poll_interval=0.2, client=None):
        self.client = client if client is not None else ModbusTcpClient(
            host, port=port, timeout=io_timeout, retries=0,
        )
        self.device_id = device_id
        self.ready_timeout = ready_timeout
        self.start_timeout = start_timeout
        self.action_timeout = action_timeout
        self.poll_interval = poll_interval
        # 在发送动作前置位；即便写响应丢失，也尝试清除可能已生效的命令。
        self.command_active = False
        self.observer = None
        self.cancel_check = None

    def checkpoint(self):
        if self.cancel_check is not None:
            self.cancel_check()

    def set_observer(self, callback):
        """UI 只接收事件，沿用单线程 Modbus 通信。"""
        self.observer = callback

    def emit(self, kind, **fields):
        if self.observer is not None:
            try:
                self.observer(kind, **fields)
            except Exception:
                # 显示失败不改变机械动作，也不影响命令清零与连接关闭。
                self.observer = None

    def connect(self):
        if not self.client.connect():
            raise WarehouseError("PLC 连接失败")
        self.emit("connected")

    @staticmethod
    def check(response, operation):
        if response is None or response.isError():
            raise WarehouseError(f"{operation}失败：{response}")
        return response

    def read(self, address):
        self.checkpoint()
        if address not in (STATUS, READY):
            raise ValueError("只允许读取设备状态或就绪状态")
        result = self.check(self.client.read_holding_registers(
            address, count=1, device_id=self.device_id,
        ), f"读取 {40001 + address}")
        if len(result.registers) != 1:
            raise WarehouseError("PLC 读取响应的寄存器数量不正确")
        value = result.registers[0]
        self.emit("register", address=address, value=value)
        return value

    def write(self, address, value):
        self.checkpoint()
        limits = {COLUMN: (1, 6), LEVEL: (1, 3), WAREHOUSE: (1, 1), COMMAND: (0, 4)}
        if address not in limits:
            raise ValueError("禁止写入只读或未定义的寄存器")
        low, high = limits[address]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{40001 + address} 的值须在 {low}～{high} 范围内")
        self.check(self.client.write_register(
            address, value, device_id=self.device_id,
        ), f"写入 {40001 + address}={value}")
        log(f"PLC 写入 {40001 + address}（地址 {address}）= {value}")

    def status(self):
        value = self.read(STATUS)
        if value == 2:
            raise WarehouseError("设备未回零，请在设备端完成回零后重新调度")
        if value not in (0, 1):
            raise WarehouseError(f"设备状态值异常：{value}")
        if self.observer is not None:
            self.read(READY)  # 彩色面板运行期间同步刷新就绪，不另开通信线程。
        return value

    def wait_available(self):
        log("等待 PLC 空闲且仓库就绪")
        self.emit("waiting", stage="等待 PLC 空闲且仓库就绪")
        deadline = time.monotonic() + self.ready_timeout
        while True:
            state = self.status()
            ready = self.read(READY)
            if ready not in (0, 1):
                raise WarehouseError(f"仓库就绪状态值异常：{ready}")
            if state == 0 and ready == 1:
                return
            if time.monotonic() >= deadline:
                raise WarehouseError(f"等待空闲/就绪超时，设备状态={state}，就绪={ready}")
            time.sleep(self.poll_interval)

    def wait_status(self, expected, timeout, stage):
        self.emit("waiting", stage=stage)
        deadline = time.monotonic() + timeout
        while True:
            current = self.status()
            if current == expected:
                return
            if time.monotonic() >= deadline:
                raise WarehouseError(f"{stage}超时，最后设备状态={current}；不自动重发动作")
            time.sleep(self.poll_interval)

    def clear_command(self):
        self.write(COMMAND, 0)
        self.command_active = False

    def begin(self, command, column=None, level=None):
        if type(command) is not int or command not in (1, 2, 3, 4):
            raise ValueError("不支持的 PLC 动作命令")
        if self.command_active:
            raise WarehouseError("上一条命令尚未结束")
        if command in (1, 4):
            if type(column) is not int or not 1 <= column <= 6:
                raise ValueError("列号必须为 1～6")
            if type(level) is not int or not 1 <= level <= 3:
                raise ValueError("层号必须为 1～3")
        self.wait_available()
        self.clear_command()
        if command in (1, 4):
            # 参数分别写入，全部成功后，最后下发动作命令。
            self.write(COLUMN, column)
            self.write(LEVEL, level)
            self.write(WAREHOUSE, 1)
        self.command_active = True
        self.emit("action", command=command, column=column, level=level)
        self.write(COMMAND, command)

    def wait_started(self):
        self.wait_status(1, self.start_timeout, "等待动作进入忙碌状态")
        log("PLC 已进入忙碌状态")

    def finish(self):
        self.wait_status(0, self.action_timeout, "等待动作完成")
        self.clear_command()
        log("PLC 已恢复空闲，动作完成")

    def move(self, command, column, level):
        if command not in (1, 4):
            raise ValueError("货叉动作仅支持命令 1（出库）或 4（入库）")
        log(f"A 库{'入库' if command == 4 else '出库'}：{level} 层 {column} 列")
        self.begin(command, column, level)
        self.wait_started()
        self.finish()

    def line_inbound(self):
        """命令 3：皮带开进，PLC 检测到物料后停止；检测不到则超时清零。"""
        log("开始线体入货")
        self.begin(3)
        self.wait_started()
        self.finish()
        log("线体入货结束（PLC 忙碌后恢复空闲）")

    def line_outbound(self, duration):
        """命令 2：按用户指定时长开出，到时清零；不推断物料已经交接。"""
        if not math.isfinite(duration) or not 0 < duration <= self.action_timeout:
            raise ValueError("线体出货运行秒数须为有限正数，且不超过动作超时")
        log(f"开始线体出货，进入忙碌后最多运行 {duration:g} 秒")
        self.begin(2)
        self.wait_started()
        self.emit("waiting", stage=f"线体出货运行中，最多 {duration:g} 秒")
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if self.status() == 0:
                log("PLC 提前恢复空闲，结束出货运行")
                break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.poll_interval, remaining))
        self.clear_command()
        self.wait_status(0, self.action_timeout, "等待线体出货停止")
        log("线体出货运行已停止，请现场确认物料是否完全出货")

    def close(self):
        # 清 0 是撤销命令/停止皮带机的尽力处理，不等同于机械急停。
        cleared = True
        try:
            if self.command_active:
                try:
                    self.clear_command()
                except Exception as exc:
                    cleared = False
                    log(f"命令清 0 失败：{exc}。设备状态未确认，请在现场检查并停止设备。")
        finally:
            self.client.close()
            self.emit("disconnected")
        return cleared
