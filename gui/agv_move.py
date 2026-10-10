"""AGV 单体到站控制：只使用 AGV 连接，复用既有协议与严格到站检查。"""

from dataclasses import asdict
from types import SimpleNamespace
import math
import time
from uuid import uuid4

import outbound_agv as agv

from .models import EventKind
from .real_backend import RealBackend


class RealAgvMoveBackend(RealBackend):
    def stage(self, index, status, detail):
        self.control.checkpoint()
        self.current_stage = index
        self.publish(EventKind.STAGE, stage=index, status=status, detail=detail)
        self.log(detail)

    def observe_agv(self, status):
        self.last_status = dict(status)
        moving = status["speed_percent"] != 0 or status["state_code"] not in agv.STOPPED_STATES
        if (status["alarm_io"] & 0x07 or (not moving and status["station"] != self.confirmed_station)):
            self.confirmed_station = None
        self.device(raw_station=status["station"], state=status["state"],
                    speed_percent=status["speed_percent"], in_transit=moving,
                    confirmed_station=self.confirmed_station, alarm=f"IO=0x{status['alarm_io']:02X}",
                    voltage=f"{status['voltage']} V" if "voltage" in status else "—")

    def confirmation(self, message):
        # 沿用线程安全确认桥接，原 wait_confirmation 在等待期间继续查状态。
        from .real_backend import ConfirmationReader
        request_id = uuid4().hex
        self.control.prepare_confirmation(request_id)
        self.publish(EventKind.CONFIRMATION, request_id=request_id, key="agv_prepare",
                     title="AGV 单体行走准备确认", message=message,
                     timeout=self.settings.transfer_timeout)
        return ConfirmationReader(self.control, request_id)

    def run(self):
        self.parameters.validate(self.settings)
        self.control.checkpoint()
        p, s = self.parameters, self.settings
        args = SimpleNamespace(**asdict(s))
        self.stage(0, "running", "连接 AGV，检查停车与报警")
        self.device(plc_connection="未参与", agv_connection="连接中", plc_ready=None)
        self.client = self.agv_factory(s.host, s.port, s.agv_id, s.timeout, s.query_data)
        self.client.status_observer = self.observe_agv
        try:
            self.client.connect()
            self.agv_connected = True
            self.device(agv_connection="已连接")
        except Exception:
            self.device(agv_connection="连接失败")
            raise
        hooks = agv.ExecutionHooks(self.control.checkpoint, log_sink=self.log, confirmation=self.confirmation)
        with agv.execution_hooks(hooks):
            initial = self.client.status()
            agv.require_stationary(initial)
            self.client.command(0x02, 0, "AGV 单体准备：停止行走")
            self.client.command(0x17, 0, "AGV 单体准备：停止滚筒")
            self.client.command(0x06, 0, "AGV 单体准备：清除旧目标")
            # 站点 0 保留为未知原始反馈，不将它自动当作站点 1。
            agv.wait_confirmation(self.client, initial["station"], args,
                                  f"确认滚筒已停止、货物固定且交接区已脱离，路线可通行；"
                                  f"目标站点 {p.target_station}，行走方向为{p.direction_text}。")
            self.stage(0, "completed", "AGV 停车及现场行走条件已确认")
            self.device(target_station=p.target_station)
            self.stage(1, "running", f"{p.direction_text}前往站点 {p.target_station}")
            current = self.client.status()
            agv.require_stationary(current)
            already_at_target = (current["station"] == p.target_station
                                 and current["state_code"] in {0x0E, 0x19})
            if not already_at_target:
                self.client.command(0x06, p.target_station, f"设置单体目标站点 {p.target_station}")
                self.client.command(0x01, 0 if p.direction == "forward" else 1,
                                    f"AGV {p.direction_text}运行")
            else:
                self.log("当前反馈已在目标站点停车，复核到站；不重复启动行走。")
            agv.wait_at_station(self.client, p.target_station, args, direction=p.direction_text)
            self.stage(1, "completed", f"站点 {p.target_station} 连续两次到站反馈通过")
            self.stage(2, "running", "发送停止指令并再次复核目标站点停车")
            self.client.command(0x02, 0, "单体到站：停止行走")
            agv.require_stationary(self.client.status(), p.target_station)
            self.confirmed_station = p.target_station
            self.device(confirmed_station=p.target_station, in_transit=False, target_station=None)
            self.stage(2, "completed", f"站点 {p.target_station} 到站停车已确认")


class SimulationAgvMoveBackend:
    def __init__(self, parameters, settings, control, publish, *, time_scale=0.15):
        if not math.isfinite(time_scale) or time_scale <= 0:
            raise ValueError("模拟时间比例须为有限正数。")
        self.parameters, self.settings = parameters, settings
        self.control, self.publish, self.time_scale = control, publish, time_scale

    def stage(self, index, status, detail):
        self.control.checkpoint()
        self.publish(EventKind.STAGE, stage=index, status=status, detail=detail)
        self.publish(EventKind.LOG, level="INFO", message=f"[模拟 AGV 单体] {detail}")

    def run(self):
        self.parameters.validate(self.settings)
        p = self.parameters
        self.stage(0, "running", "模拟 AGV 检查，等待行走准备确认")
        self.publish(EventKind.DEVICE, plc_connection="未参与", agv_connection="模拟模式",
                     confirmed_station=None, raw_station=1, state="待准备确认", speed_percent=0,
                     target_station=p.target_station, in_transit=False, alarm="无（模拟）", voltage="24.50 V（模拟）")
        request_id = uuid4().hex
        self.control.prepare_confirmation(request_id)
        self.publish(EventKind.CONFIRMATION, request_id=request_id, key="agv_prepare",
                     title="AGV 单体行走准备确认", message=f"这是模拟任务。目标站点 {p.target_station}，方向{p.direction_text}；请确认模拟行走条件。",
                     timeout=self.settings.transfer_timeout)
        self.control.wait_confirmation(request_id, self.settings.transfer_timeout)
        self.stage(0, "completed", "模拟行走准备确认通过")
        self.stage(1, "running", f"模拟{p.direction_text}前往站点 {p.target_station}")
        if p.target_station != 1:
            self.publish(EventKind.DEVICE, confirmed_station=1, target_station=p.target_station,
                         in_transit=True, speed_percent=30, state=f"{p.direction_text}运行（模拟）")
            self.control.wait(3 * self.time_scale)
        for _ in range(2):
            self.control.checkpoint()
            self.publish(EventKind.DEVICE, raw_station=p.target_station, speed_percent=0, state="停站中（模拟）")
            self.control.wait(self.settings.poll_interval * self.time_scale)
        self.stage(1, "completed", "模拟连续两次到站反馈通过")
        self.stage(2, "running", "模拟停止行走并复核停车")
        self.publish(EventKind.DEVICE, confirmed_station=p.target_station, target_station=None, in_transit=False)
        self.stage(2, "completed", f"站点 {p.target_station} 停车已确认（模拟）")

    def cleanup(self):
        time.sleep(min(0.15, self.time_scale))
        self.publish(EventKind.DEVICE, plc_connection="未参与", agv_connection="模拟模式",
                     target_station=None, in_transit=False, speed_percent=0, state="模拟已停止")
