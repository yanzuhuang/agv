"""单文件出库与 AGV 搬运：站点1上料2秒 -> 后退到站点2 -> 前进回站点1 -> 下料2秒。

仓库、AGV、报文编解码和停止退出全部实现在本文件。仓库通信依赖 pymodbus==3.11.4。
python outbound_agv.py --column 6 --level 1
python outbound_agv.py --operation stop   # 停止 AGV 并退出
python outbound_agv.py --operation exit   # 不连接设备，直接退出
"""

import argparse
import math
import os
import queue
import socket
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ExecutionHooks:
    """可选的调用方接口；未提供时保持控制台行为。"""
    cancel_check: object = None
    stage: object = None
    confirmation: object = None
    log_sink: object = None


_EXECUTION_HOOKS = ContextVar("agv_execution_hooks", default=None)


@contextmanager
def execution_hooks(hooks):
    token = _EXECUTION_HOOKS.set(hooks)
    try:
        yield
    finally:
        _EXECUTION_HOOKS.reset(token)


def checkpoint():
    hooks = _EXECUTION_HOOKS.get()
    if hooks is not None and hooks.cancel_check is not None:
        hooks.cancel_check()


def stage_event(index, status, detail):
    checkpoint()
    hooks = _EXECUTION_HOOKS.get()
    if hooks is not None and hooks.stage is not None:
        hooks.stage(index, status, detail)


AGV_HOST = "192.168.1.16"


AGV_PORT = 1024


FRAME_SIZE = 16


STATE_NAMES = {
    0x00: "脱机", 0x01: "复位/空闲", 0x02: "运行", 0x03: "机械防撞",
    0x04: "红外防撞", 0x05: "定时停", 0x06: "急停", 0x07: "出轨",
    0x08: "错误", 0x09: "交管状态", 0x0A: "前向运行", 0x0B: "后向运行",
    0x0C: "前出轨", 0x0D: "后出轨", 0x0E: "停站中", 0x0F: "充电中",
    0x10: "左转", 0x11: "右转", 0x12: "待命中", 0x13: "换前向停",
    0x14: "换后向停", 0x15: "换反向停", 0x16: "逆时针旋转",
    0x17: "顺时针旋转", 0x18: "对接中", 0x19: "对接完成",
    0x20: "左横移", 0x21: "右横移",
}


def hex_text(data):
    return data.hex(" ").upper()


def build_frame(agv_id, function, data=0):
    """数据低位在前，数据码#2及8字节预留置0，校验覆盖AA至FF。"""
    for value, low, high, name in (
        (agv_id, 1, 255, "AGV ID"),
        (function, 0, 255, "功能码"),
        (data, 0, 65535, "数据码"),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} 必须为 {low}～{high} 的整数")
    body = bytes((0xAA, agv_id, function, data & 0xFF, data >> 8)) + bytes(9) + b"\xFF"
    return body + bytes((sum(body) % 256,))


def validate_frame(frame):
    if len(frame) != FRAME_SIZE:
        raise ValueError("AGV 报文长度必须为 16 字节")
    if frame[0] != 0xAA or frame[14] != 0xFF:
        raise ValueError("AGV 报文帧头或帧尾错误")
    if frame[15] != sum(frame[:15]) % 256:
        raise ValueError("AGV 报文校验和错误")


class FrameDecoder:
    """保留不完整帧，拆分粘连帧，跳过噪声及错误帧重新找AA。"""

    def __init__(self):
        self.buffer = bytearray()
        self.discarded = 0

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            start = self.buffer.find(b"\xAA")
            if start < 0:
                self.discarded += len(self.buffer)
                self.buffer.clear()
                break
            self.discarded += start
            del self.buffer[:start]
            if len(self.buffer) < FRAME_SIZE:
                break
            frame = bytes(self.buffer[:FRAME_SIZE])
            try:
                validate_frame(frame)
            except ValueError:
                self.discarded += 1
                del self.buffer[0]
                continue
            frames.append(frame)
            del self.buffer[:FRAME_SIZE]
        return frames


def parse_status(frame):
    """PDF 2.5.1：地标号高位在前；电量字段实际表示电压，不是百分比。"""
    validate_frame(frame)
    if frame[2] != 0x05:
        raise ValueError("该帧不是 AGV 状态帧")
    return {
        "agv_id": frame[1],
        "landmark": int.from_bytes(frame[3:5], "big"),
        "station": frame[5],
        "speed_percent": frame[6],
        "state_code": frame[7],
        "state": STATE_NAMES.get(frame[7], f"未知状态 0x{frame[7]:02X}"),
        "hook": {1: "升", 2: "降"}.get(frame[8], f"未知值 0x{frame[8]:02X}"),
        "voltage": f"{frame[9]}.{frame[10]:02d}",
        "alarm_io": frame[11],
        "active_inputs": [bit + 1 for bit in range(8) if frame[12] & (1 << bit)],
    }


# 两份设备文档的名称相反，必须按实际设备明确选择。
# 现场上料方向修正为反转；下料仍使用已验证的正转指令。
ROLLER_PROFILES = {"word": (4, 1), "pdf": (2, 3)}
STOPPED_STATES = {0x01, 0x0E, 0x12, 0x19}
TRAVEL_STATES = STOPPED_STATES | {0x02, 0x0B, 0x14, 0x18}
FORWARD_STATES = STOPPED_STATES | {0x02, 0x0A, 0x13, 0x18}
LOAD_SECONDS = 2.0
UNLOAD_SECONDS = 2.0
LOAD_LEAD_SECONDS = LOAD_SECONDS
LINE_DURATION = 5.0


class AGVError(RuntimeError):
    pass


class Cancelled(AGVError):
    pass


def log(message):
    hooks = _EXECUTION_HOOKS.get()
    if hooks is not None and hooks.log_sink is not None:
        hooks.log_sink(message)
        return
    print(message, flush=True)


class AGVClient:
    """一条连接串行通信；动作无应答时不重发，保留拆包缓冲区。"""

    def __init__(self, host=AGV_HOST, port=AGV_PORT, agv_id=1, timeout=5, query_data=1):
        self.host, self.port, self.agv_id = host, port, agv_id
        self.timeout, self.query_data = timeout, query_data
        self.socket = None
        self.decoder = FrameDecoder()
        self.broken = False
        self.status_observer = None

    def connect(self):
        self.socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.decoder = FrameDecoder()
        self.broken = False

    def close(self):
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None

    def request(self, function, data=0, timeout=None):
        checkpoint()
        if self.socket is None or self.broken:
            raise AGVError("连接不可用；动作不会自动重发")
        frame = build_frame(self.agv_id, function, data)
        limit = self.timeout if timeout is None else min(self.timeout, timeout)
        deadline = time.monotonic() + limit
        try:
            self.socket.settimeout(limit)
            self.socket.sendall(frame)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待 AGV 应答超时")
                self.socket.settimeout(remaining)
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise ConnectionError("AGV 已断开连接")
                matched = None
                for reply in self.decoder.feed(chunk):
                    if reply[1:3] == frame[1:3] and matched is None:
                        matched = reply
                if matched is not None:
                    if function != 0x05:
                        log(f"应答：功能码 0x{function:02X}，数据码 {int.from_bytes(matched[3:5], 'little')}（仅收到应答）")
                    return matched
        except (OSError, KeyboardInterrupt):
            # 即使 sendall 失败，也可能已发送部分/全部字节。
            self.broken = True
            raise

    def command(self, function, data, label):
        log(f"{label}；HEX: {hex_text(build_frame(self.agv_id, function, data))}")
        return self.request(function, data)

    def status(self, timeout=None):
        result = parse_status(self.request(0x05, self.query_data, timeout))
        if self.status_observer is not None:
            try:
                self.status_observer(dict(result))
            except Exception:
                # 显示失败不改变控制逻辑或阻止退出清理。
                self.status_observer = None
        return result

    def shutdown(self):
        """即使一条停止指令失败，也尝试其余指令；最后总是关闭连接。"""
        ok = True
        self.shutdown_report = {"travel_stop_ack": False, "roller_stop_ack": False,
                                "target_clear_ack": False, "travel_stationary": False,
                                "roller_stationary": None, "connection_closed": False}
        try:
            for function, data, label in (
                (0x02, 0, "退出：停止行走"),
                (0x17, 0, "退出：停止滚筒"),
                (0x06, 0, "退出：清除目标站点"),
            ):
                try:
                    if self.socket is None or self.broken:
                        self.close()
                        self.connect()
                    self.command(function, data, label)
                    key = {0x02: "travel_stop_ack", 0x17: "roller_stop_ack", 0x06: "target_clear_ack"}[function]
                    self.shutdown_report[key] = True
                except (OSError, AGVError, KeyboardInterrupt) as exc:
                    ok = False
                    self.broken = True
                    log(f"{label}未确认：{exc}")
            if not self.broken:
                try:
                    status = self.status()
                    require_stationary(status)
                    self.shutdown_report["travel_stationary"] = True
                    log(f"已确认行走停止，当前站点={status['station']}。滚筒停止需现场核对。")
                except (OSError, AGVError, ValueError, KeyboardInterrupt) as exc:
                    ok = False
                    log(f"退出时未确认停车状态：{exc}")
        finally:
            self.close()
            self.shutdown_report["connection_closed"] = True
            log("TCP 连接已关闭，程序退出。")
        return ok


def require_stationary(status, station=None, *, allow_initial_zero=False):
    if status["alarm_io"] & 0x07:
        raise AGVError(f"AGV 报警 IO=0x{status['alarm_io']:02X}")
    if status["speed_percent"] != 0 or status["state_code"] not in STOPPED_STATES:
        raise AGVError(f"AGV 未处于可装卸的停车状态：{status['state']}，速度={status['speed_percent']}%")
    # Only the site-confirmed initial position at station 1 accepts feedback 0.
    # Keep the raw feedback and strict arrival checks after driving.
    initial_zero = allow_initial_zero and station == 1 and status["station"] == 0
    if station is not None and status["station"] != station and not initial_zero:
        raise AGVError(f"需要实际停在站点{station}，当前站点={status['station']}；程序不改写实际位置")


def loaded(status, args):
    level = int(args.material_input in status["active_inputs"])
    return level == args.loaded_level


class ConsoleConfirmation:
    """非阻塞读取控制台，不遗留超时后仍等待 input 的后台线程。"""

    def get(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.name == "nt":
                import msvcrt
                if msvcrt.kbhit():
                    character = msvcrt.getwch()
                    if character == "\x03":
                        raise KeyboardInterrupt
                    if character in ("\x00", "\xe0"):
                        continue
                    log("")
                    return "" if character in ("\r", "\n") else character.lower()
            else:
                import select
                if select.select([sys.stdin], [], [], 0)[0]:
                    line = sys.stdin.readline()
                    return line.strip().lower() if line else "q"
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        raise queue.Empty


def confirmation_reader(message):
    hooks = _EXECUTION_HOOKS.get()
    if hooks is not None and hooks.confirmation is not None:
        return hooks.confirmation(message)
    if not sys.stdin.isatty():
        raise AGVError("现场确认需要交互控制台；请在 PowerShell 终端运行，不要重定向标准输入")
    log(message + "（完成按回车；q 退出）：")
    return ConsoleConfirmation()


def wait_at_station(client, station, args, direction="后退"):
    deadline = time.monotonic() + args.travel_timeout
    stable = 0
    previous = None
    while True:
        checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AGVError(f"等待站点{station}超时")
        status = client.status(remaining)
        snapshot = (status["station"], status["state_code"], status["speed_percent"])
        if snapshot != previous:
            log(f"行走状态：站点={status['station']}，{status['state']}，速度={status['speed_percent']}%")
            previous = snapshot
        allowed_states = FORWARD_STATES if direction == "前进" else TRAVEL_STATES
        if status["alarm_io"] & 0x07 or status["state_code"] not in allowed_states:
            raise AGVError(f"{direction}途中异常：{status['state']}，报警 IO=0x{status['alarm_io']:02X}")
        # 必须是目标站点 + 停站/对接完成 + 零速度；站点号可能在行走中保持旧值。
        arrived = (status["station"] == station and status["speed_percent"] == 0
                   and status["state_code"] in {0x0E, 0x19})
        stable = stable + 1 if arrived else 0
        if stable >= 2:
            return
        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))


def wait_confirmation(client, station, args, message, monitor=None, *, allow_initial_zero=False):
    answers = confirmation_reader(message)
    deadline = time.monotonic() + args.transfer_timeout
    while True:
        checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AGVError("等待现场确认超时")
        require_stationary(client.status(remaining), station, allow_initial_zero=allow_initial_zero)
        if monitor is not None:
            monitor()
        try:
            answer = answers.get(timeout=min(args.poll_interval, max(0, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if answer != "":
            raise Cancelled("现场取消任务")
        return


def wait_timed(deadline, feeder=None, feed_deadline=None):
    """按绝对时间处理线体停止与滚筒停止，避免逐段 sleep 累加通信耗时。"""
    feed_stopped = feeder is None
    while True:
        checkpoint()
        now = time.monotonic()
        if not feed_stopped and now >= feed_deadline:
            # 这里只发送清0；确认线体空闲放在滚筒停止后，避免拖长上料时间。
            feeder.stop_feed(wait=False)
            feed_stopped = True
            now = time.monotonic()
        if now >= deadline:
            return
        next_deadline = deadline if feed_stopped else min(deadline, feed_deadline)
        time.sleep(min(0.05, max(0, next_deadline - now)))


def transfer(client, station, is_loading, args, feeder=None, *, allow_initial_zero=False):
    if feeder is not None and is_loading and (not math.isfinite(args.line_duration) or args.line_duration <= LOAD_SECONDS):
        raise ValueError("线体出货运行时间必须为大于2秒的有限数")
    status = client.status()
    require_stationary(status, station, allow_initial_zero=allow_initial_zero)
    if args.material_input is not None and loaded(status, args) == is_loading:
        raise AGVError("装卸开始前物料状态不符：上料前须无料，下料前须有料")
    load_data, unload_data = ROLLER_PROFILES[args.roller_profile]
    label = "反转上料" if is_loading else "正转下料"
    duration = LOAD_SECONDS if is_loading else UNLOAD_SECONDS
    feed_deadline = None
    if feeder is not None:
        if is_loading:
            feeder.start_feed()
            # 只有出货按时长停止；从观察到线体忙碌开始计时。
            feed_deadline = time.monotonic() + args.line_duration
            log(f"线体出货运行{args.line_duration:g}秒，最后{LOAD_SECONDS:g}秒AGV上料。")
            wait_timed(feed_deadline - LOAD_LEAD_SECONDS)
            if time.monotonic() >= feed_deadline:
                raise AGVError("已错过线体上料时机，任务终止")
        else:
            feeder.start_receive()
            log(f"线体入货由 PLC 物料检测结束，AGV 同时下料{UNLOAD_SECONDS:g}秒。")
    # 计时包含应答等待，不能收到应答后再额外延长运行时间。
    roller_deadline = time.monotonic() + duration
    if feeder is not None and is_loading:
        roller_deadline = feed_deadline
    client.command(0x17, load_data if is_loading else unload_data, f"{label}，运行{duration:g}秒")
    wait_timed(roller_deadline, feeder if is_loading else None, feed_deadline)
    client.command(0x17, 0, f"{label}{duration:g}秒结束：停止滚筒")
    if feeder is not None:
        if is_loading:
            feeder.wait_feed_stopped()
        else:
            # start_receive 已确认忙碌；先等 PLC 空闲完成，再清零。
            # action_timeout 仅限制等待上限，不是线体运行时长。
            feeder.finish_receive()
    status = client.status()
    require_stationary(status, station, allow_initial_zero=allow_initial_zero)
    if args.material_input is not None and loaded(status, args) != is_loading:
        raise AGVError("停止滚筒后物料状态不符")
    if is_loading and args.material_input is None:
        wait_confirmation(
            client, station, args,
            "上料计时结束，不代表上料成功。请现场确认滚筒已停止、货物完全上车且已与货架脱离，允许车辆后退；未完成请按 q 退出",
            allow_initial_zero=allow_initial_zero,
        )


def deliver(client, args, warehouse=None):
    initial = client.status()
    require_stationary(initial, 1, allow_initial_zero=True)
    if initial["station"] == 0:
        log('初始站点反馈=0，按现场已确认的站点1起点处理；仍检查停车和报警。')
    if args.material_input is not None and loaded(initial, args):
        raise AGVError("AGV 已有料，不能开始新的出库取货任务")
    client.command(0x02, 0, "站点1：停止行走")
    client.command(0x17, 0, "站点1：停止滚筒")
    client.command(0x06, 0, "清除之前的目标站点")
    require_stationary(client.status(), 1, allow_initial_zero=True)
    wait_confirmation(client, 1, args,
                      "确认AGV空载、源库位有货，站点1可上料及接收返回后的下料" if warehouse is not None
                      else "确认货物已放到线体、输送条件就绪，站点1可接收返回后的下料",
                      allow_initial_zero=True)
    if warehouse is not None:
        warehouse.outbound()
    transfer(client, 1, True, args, warehouse, allow_initial_zero=True)
    client.command(0x06, 2, "设置目标站点2")
    client.command(0x01, 1, "从站点1后退到站点2")
    wait_at_station(client, 2, args)
    client.command(0x02, 0, "站点2：停止行走")
    require_stationary(client.status(), 2)
    client.command(0x06, 0, "清除旧目标站点，准备返回站点1")
    client.command(0x06, 1, "设置返回目标站点1")
    client.command(0x01, 0, "从站点2前进到站点1")
    wait_at_station(client, 1, args, direction="前进")
    client.command(0x02, 0, "返回站点1：停止行走")
    require_stationary(client.status(), 1)
    transfer(client, 1, False, args, warehouse)
    log("定时流程结束：站点1上料 -> 后退到站点2 -> 前进回站点1 -> 下料。请核对货物位置。")


class WarehouseOutbound:
    def __init__(self, args, client=None):
        if client is None:
            # AGV 单独运行/离线预览无需安装 pymodbus。
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(args.plc_host, port=args.plc_port,
                                     timeout=args.timeout, retries=0)
        self.client = client
        self.args = args
        self.active = False

    def connect(self):
        if not self.client.connect():
            raise AGVError("仓库 PLC 连接失败")

    def read(self):
        reply = self.client.read_holding_registers(0, count=6, device_id=self.args.device_id)
        if reply.isError() or len(reply.registers) != 6:
            raise AGVError("读取仓库状态失败")
        state, ready = reply.registers[0], reply.registers[5]
        if state == 2:
            raise AGVError("仓库未回零")
        if state not in (0, 1) or ready not in (0, 1):
            raise AGVError(f"仓库状态异常：状态={state}，就绪={ready}")
        return state, ready

    def write(self, address, value):
        reply = self.client.write_register(address, value, device_id=self.args.device_id)
        if (reply.isError() or reply.address != address
                or reply.registers != [value]):
            raise AGVError(f"仓库寄存器{40001 + address}写入未确认")

    def wait(self, predicate, timeout, label):
        deadline = time.monotonic() + timeout
        while True:
            state, ready = self.read()
            if time.monotonic() >= deadline:
                raise AGVError(f"{label}超时：状态={state}，就绪={ready}")
            if predicate(state, ready):
                return
            time.sleep(min(self.args.poll_interval, max(0, deadline - time.monotonic())))

    def ready(self):
        self.wait(lambda state, ready: state == 0 and ready == 1,
                  self.args.ready_timeout, "等待仓库空闲且就绪")

    def start(self, command):
        # 写响应丢失仍可能执行动作，先记下清零责任；不自动重发。
        self.active = True
        self.write(4, command)
        self.wait(lambda state, ready: state == 1,
                  self.args.start_timeout, "等待仓库进入忙碌")

    def clear(self):
        self.write(4, 0)
        self.active = False

    def outbound(self):
        self.ready()
        self.clear()
        self.write(1, self.args.column)
        self.write(2, self.args.level)
        self.write(3, 1)
        log(f"仓库出库：A库第{self.args.level}层、第{self.args.column}列 -> 线体")
        self.start(1)
        self.wait(lambda state, ready: state == 0,
                  self.args.action_timeout, "等待仓库出库完成")
        self.clear()
        log("仓库出库完成，货物已放到线体。")

    def start_feed(self):
        self.ready()
        self.clear()
        log("开启仓库线体出货（命令2），AGV等待至线体剩余2秒时上料。")
        self.start(2)

    def start_receive(self):
        self.ready()
        self.clear()
        log("开启仓库线体入货（命令3），随即启动AGV下料2秒。")
        self.start(3)

    def finish_receive(self):
        self.wait(lambda state, ready: state == 0,
                  self.args.action_timeout, "等待线体入货物料到位")
        self.clear()
        log("线体入货已由 PLC 确认完成，命令清0。")

    def check_feed(self):
        # 就绪只用于动作开始前；设备忙碌时就绪位可能变0。
        self.read()

    def stop_feed(self, wait=True):
        self.clear()
        log("线体运行时间结束，仓库命令已清0。")
        if wait:
            self.wait_feed_stopped()

    def wait_feed_stopped(self):
        self.wait(lambda state, ready: state == 0,
                  self.args.start_timeout, "等待仓库线体停止")
        log("仓库线体命令已清0，已确认空闲。")

    def close(self):
        ok = True
        try:
            if self.active:
                try:
                    self.stop_feed()
                except Exception as exc:
                    ok = False
                    log(f"仓库命令清0/停止未确认：{exc}")
        finally:
            self.client.close()
        return ok


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=AGV_HOST)
    parser.add_argument("--port", type=int, default=AGV_PORT)
    parser.add_argument("--agv-id", type=int, default=1)
    parser.add_argument("--operation", choices=("deliver", "stop", "exit"), default="deliver", help="deliver 出库搬运（默认）；stop 停止AGV并退出；exit 直接退出")
    parser.add_argument("--roller-profile", choices=ROLLER_PROFILES, help="word：反转上料04/下料01；pdf：反转上料02/下料03，按设备确认后选择")
    parser.add_argument("--material-input", type=int, help="可选：物料输入IO 1～8，用于上料2秒、下料2秒前后的校验")
    parser.add_argument("--loaded-level", type=int, choices=(0, 1), help="有料时输入电平；使用物料 IO 时必须指定")
    parser.add_argument("--query-data", type=int, choices=(0, 1), default=1)
    parser.add_argument("--timeout", type=float, default=5, help="每次通信超时秒数")
    parser.add_argument("--travel-timeout", type=float, default=180)
    parser.add_argument("--transfer-timeout", type=float, default=120, help="开始任务前现场准备确认的上限秒数；上料2秒、下料2秒")
    parser.add_argument("--line-duration", type=float, default=LINE_DURATION, help="线体出货运行秒数，默认5，须大于2秒；上料重叠最后2秒，入货由PLC物料检测结束")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true", help="预览流程，完全不连接设备")
    parser.add_argument("--warehouse-outbound", dest="warehouse_outbound", action="store_true", default=True, help="仓库出库与AGV联动（搬运的默认模式）")
    parser.add_argument("--agv-only", dest="warehouse_outbound", action="store_false", help="仅控制AGV，仓库出库和线体由现场配合")
    parser.add_argument("--column", type=int, help="源库位列号1～6；联动模式必须指定")
    parser.add_argument("--level", type=int, help="源库位层号1～3；联动模式必须指定")
    parser.add_argument("--plc-host", default="192.168.1.11")
    parser.add_argument("--plc-port", type=int, default=502)
    parser.add_argument("--device-id", type=int, default=1, help="仓库PLC Slave ID")
    parser.add_argument("--ready-timeout", type=float, default=30)
    parser.add_argument("--start-timeout", type=float, default=15)
    parser.add_argument("--action-timeout", type=float, default=180)
    args = parser.parse_args(argv)
    # 停止/直接退出无需源库位，也不启动仓库联动。
    args.warehouse_outbound = args.warehouse_outbound and args.operation == "deliver"
    args.host = args.host.strip()
    if not args.host or not 1 <= args.port <= 65535 or not 1 <= args.agv_id <= 255:
        parser.error("地址不能为空，端口须1～65535，AGV ID须1～255")
    for name in ("timeout", "travel_timeout", "transfer_timeout", "poll_interval",
                 "ready_timeout", "start_timeout", "action_timeout", "line_duration"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"{name} 须为有限正数")
    if args.line_duration <= LOAD_LEAD_SECONDS:
        parser.error("--line-duration 必须大于2秒")
    if args.material_input is not None and not 1 <= args.material_input <= 8:
        parser.error("物料输入 IO 编号须1～8")
    if (args.material_input is None) != (args.loaded_level is None):
        parser.error("--material-input 与 --loaded-level 必须一起提供")
    if not args.plc_host.strip() or not 1 <= args.plc_port <= 65535 or not 1 <= args.device_id <= 247:
        parser.error("PLC地址不能为空，端口须1～65535，Slave ID须1～247")
    args.plc_host = args.plc_host.strip()
    if args.warehouse_outbound:
        if args.column is None or not 1 <= args.column <= 6 or args.level is None or not 1 <= args.level <= 3:
            parser.error("联动出库必须提供 --column 1～6 和 --level 1～3")
    elif args.column is not None or args.level is not None:
        parser.error("库位参数只用于仓库出库联动，不适用于 --agv-only、stop 或 exit")
    if args.operation == "deliver" and args.roller_profile is None:
        if args.dry_run:
            parser.error("搬运预览须指定 --roller-profile word 或 pdf")
        selection = input("按实际设备选择滚筒定义：word 反转上料04/下料01；pdf 反转上料02/下料03；q 退出：").strip().lower()
        if selection not in ROLLER_PROFILES:
            raise Cancelled("未选择已确认的滚筒定义，未连接设备")
        args.roller_profile = selection
    return args


def print_plan(args):
    log(f"目标 {args.host}:{args.port}，AGV ID={args.agv_id}；设备子网掩码255.255.0.0")
    if args.operation == "deliver":
        if args.warehouse_outbound:
            log(f"仓库 {args.plc_host}:{args.plc_port}，Slave ID={args.device_id}，A库{args.level}层{args.column}列")
            log("站点1准备确认 -> 仓库出库(命令1) -> 等待忙碌后空闲 -> 清0")
            log(f"上料：线体出货{args.line_duration:g}秒，最后2秒AGV上料，两者到时停止。")
            log("下料：线体入货与AGV同时启动，2秒停滚筒；等待PLC物料到位后空闲，再清零。")
        load_data, unload_data = ROLLER_PROFILES[args.roller_profile]
        log(f"实际站点1停车 -> 货物准备确认 -> 反转上料{LOAD_SECONDS:g}秒(17/{load_data:02X}) -> 滚筒停止")
        log("目标站点2(06/02) -> 后退(01/01) -> 确认站点2停车")
        log(f"目标站点1(06/01) -> 前进(01/00) -> 确认站点1停车 -> 正转下料{UNLOAD_SECONDS:g}秒(17/{unload_data:02X})")
        log("完成后停止行走、停止滚筒、清除目标站点并断开TCP。")
        log("上料固定运行2秒、下料固定运行2秒，到时停止；未配置物料 IO 时，上料后须现场确认才能后退。")
        if args.material_input is not None:
            log(f"装卸前后校验：输入IO #{args.material_input}，有料电平={args.loaded_level}")
        if not args.warehouse_outbound:
            log("仅AGV模式：线体由现场控制，准备确认后立即上料；与线体装卸的自动同步需使用默认联动模式。")
    elif args.operation == "stop":
        log("停止行走(02/00) -> 停止滚筒(17/00) -> 清除目标站点(06/00) -> 断开TCP")
    else:
        log("直接退出，不连接设备。")


def main(argv=None):
    try:
        args = parse_args(argv)
    except (Cancelled, EOFError, KeyboardInterrupt) as exc:
        log(f"输入取消，未连接设备：{exc}")
        return 1
    print_plan(args)
    if args.dry_run or args.operation == "exit":
        return 0
    client = AGVClient(args.host, args.port, args.agv_id, args.timeout, args.query_data)
    result = 0
    connected = False
    warehouse = None
    try:
        client.connect()
        connected = True
        if args.warehouse_outbound:
            warehouse = WarehouseOutbound(args)
            warehouse.connect()
        if args.operation == "deliver":
            deliver(client, args, warehouse)
    except (OSError, AGVError, ValueError, ImportError, KeyboardInterrupt) as exc:
        result = 1
        log(f"任务终止：{exc}；不会自动重发搬运指令。")
    except Exception as exc:
        # 包括 pymodbus 的通信异常；仍执行双方清理。
        result = 1
        log(f"任务终止：{type(exc).__name__}: {exc}；不会自动重发搬运指令。")
    finally:
        try:
            if warehouse is not None and not warehouse.close():
                result = 1
        except (Exception, KeyboardInterrupt) as exc:
            result = 1
            log(f"仓库退出清理未确认：{exc}")
        finally:
            if connected:
                if not client.shutdown():
                    result = 1
            else:
                client.close()
    if result:
        log("请现场核对设备和物料状态；通信断开不代表设备停止。")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
