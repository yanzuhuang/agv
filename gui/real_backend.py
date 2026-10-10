"""真实设备适配器：复用完整调度，同一 Worker 串行通信，不重发搬运动作。"""

from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import outbound_agv as agv
import warehouse_agv as workflow
import warehouse_modbus as warehouse
import warehouse_plc as plc_module

from .models import EventKind


class CleanupError(RuntimeError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


class ConfirmationReader:
    def __init__(self, control, request_id):
        self.control, self.request_id = control, request_id

    def get(self, timeout):
        return self.control.poll_confirmation(self.request_id, timeout)


class RealBackend:
    def __init__(self, parameters, settings, control, publish, *,
                 agv_factory=agv.AGVClient, plc_factory=warehouse.WarehousePLC):
        self.parameters, self.settings = parameters, settings
        self.control, self.publish = control, publish
        self.agv_factory, self.plc_factory = agv_factory, plc_factory
        self.client = self.plc = None
        self.agv_connected = False
        self.plc_connected = False
        self.current_stage = 0
        self.confirmed_station = None
        self.last_status = None

    def log(self, message, level="INFO"):
        self.publish(EventKind.LOG, level=level, message=f"[真实] {message}")

    def device(self, **values):
        self.publish(EventKind.DEVICE, **values)

    def stage(self, index, status, detail):
        self.current_stage = index
        if status == "running" and index in (3, 4):
            self.device(target_station=2 if index == 3 else 1)
        if status == "completed" and index in (0, 3, 4):
            self.confirmed_station = 2 if index == 3 else 1
            self.device(confirmed_station=self.confirmed_station, target_station=None, in_transit=False)
        self.publish(EventKind.STAGE, stage=index, status=status, detail=detail)
        self.log(detail)

    def observe_agv(self, status):
        self.last_status = dict(status)
        moving = status["speed_percent"] != 0 or status["state_code"] not in agv.STOPPED_STATES
        if status["alarm_io"] & 0x07:
            self.confirmed_station = None
        elif not moving and self.confirmed_station is not None:
            initial_zero = self.current_stage <= 2 and self.confirmed_station == 1 and status["station"] == 0
            if status["station"] != self.confirmed_station and not initial_zero:
                self.confirmed_station = None
        self.device(raw_station=status["station"], state=status["state"],
                    speed_percent=status["speed_percent"], in_transit=moving,
                    confirmed_station=self.confirmed_station,
                    alarm=f"IO=0x{status['alarm_io']:02X}",
                    voltage=f"{status['voltage']} V" if "voltage" in status else "—")

    def observe_plc(self, kind, **fields):
        if kind == "register":
            address, value = fields["address"], fields["value"]
            if address == plc_module.STATUS:
                self.device(plc_state={0: "空闲", 1: "忙碌", 2: "未回零"}.get(value, f"异常值 {value}"))
            elif address == plc_module.READY:
                self.device(plc_ready=value == 1, plc_ready_raw=value)
        elif kind == "waiting":
            self.log(fields["stage"])

    def confirmation(self, message):
        request_id = uuid4().hex
        key = "prepare" if self.current_stage == 0 else "loaded"
        title = "现场准备确认" if key == "prepare" else "上料完成确认"
        self.control.prepare_confirmation(request_id)
        self.log(f"等待人工确认：{title}；等待期间继续检查 AGV 停车与报警。", "WARNING")
        self.publish(EventKind.CONFIRMATION, request_id=request_id, key=key,
                     title=title, message=message, timeout=self.settings.transfer_timeout)
        return ConfirmationReader(self.control, request_id)

    def run(self):
        self.parameters.validate(self.settings)
        self.control.checkpoint()
        values = asdict(self.settings) | asdict(self.parameters)
        args = SimpleNamespace(**values, line_duration=self.parameters.duration)
        s = self.settings
        self.client = self.agv_factory(s.host, s.port, s.agv_id, s.timeout, s.query_data)
        self.client.status_observer = self.observe_agv
        self.plc = self.plc_factory(s.plc_host, s.plc_port, s.device_id,
                                    io_timeout=s.plc_io_timeout, ready_timeout=s.ready_timeout,
                                    start_timeout=s.start_timeout, action_timeout=s.action_timeout,
                                    poll_interval=s.plc_poll_interval)
        self.plc.set_observer(self.observe_plc)
        self.plc.cancel_check = self.control.checkpoint
        self.publish(EventKind.STAGE, stage=0, status="running", detail="连接 PLC 和 AGV")
        self.device(agv_connection="连接中", plc_connection="未连接")
        try:
            self.client.connect()
            self.agv_connected = True
            self.device(agv_connection="已连接")
        except Exception:
            self.device(agv_connection="连接失败")
            raise
        self.control.checkpoint()
        self.device(plc_connection="连接中")
        try:
            self.plc.connect()
            self.plc_connected = True
            self.device(plc_connection="已连接")
        except Exception:
            self.device(plc_connection="连接失败")
            raise
        hooks = agv.ExecutionHooks(self.control.checkpoint, self.stage, self.confirmation, self.log)
        token = plc_module.LOG_SINK.set(self.log)
        try:
            workflow.deliver(self.client, self.plc, args, warehouse, hooks=hooks)
        finally:
            plc_module.LOG_SINK.reset(token)

    def cleanup(self):
        # 取消不能中断停止和关闭；只允许原 shutdown 的停止指令重连。
        report = {"plc_command_clear": None, "plc_connection_closed": False,
                  "plc_was_connected": self.plc_connected,
                  "plc_command_pending": None, "plc_mechanical_stop": None, "agv": {}, "errors": []}
        token = plc_module.LOG_SINK.set(self.log)
        try:
            with agv.execution_hooks(agv.ExecutionHooks(log_sink=self.log)):
                if self.plc is not None:
                    self.plc.cancel_check = None
                    try:
                        pending = self.plc.command_active
                        cleared = bool(self.plc.close())
                        # 无待清命令或未连接时，不虚报发送过清零指令。
                        report["plc_command_clear"] = cleared if pending else None
                        report["plc_command_pending"] = bool(self.plc.command_active)
                        if not cleared:
                            report["errors"].append("PLC 命令清零未确认")
                    except Exception as exc:
                        report["errors"].append(f"PLC 清理异常：{exc}")
                    finally:
                        try:
                            self.plc.client.close()
                            report["plc_connection_closed"] = True
                            self.device(plc_connection="已断开", plc_ready=None)
                        except Exception as exc:
                            report["errors"].append(f"PLC 关闭异常：{exc}")
                            self.device(plc_connection="关闭未确认")
                if self.client is not None:
                    try:
                        if self.agv_connected:
                            if not self.client.shutdown():
                                report["errors"].append("AGV 停止指令或停车反馈未确认")
                            report["agv"] = dict(getattr(self.client, "shutdown_report", {}))
                        else:
                            self.client.close()
                    except Exception as exc:
                        report["agv"] = dict(getattr(self.client, "shutdown_report", {}))
                        report["errors"].append(f"AGV 清理异常：{exc}")
                    finally:
                        try:
                            self.client.close()
                            report["agv"]["connection_closed"] = True
                            self.device(agv_connection="已断开", target_station=None)
                        except Exception as exc:
                            report["errors"].append(f"AGV 关闭异常：{exc}")
                            self.device(agv_connection="关闭未确认")
        finally:
            plc_module.LOG_SINK.reset(token)
        def acknowledged(value):
            return "已确认" if value is True else "未确认"
        level = "ERROR" if report["errors"] else "INFO"
        clear = report["plc_command_clear"]
        clear_text = "无待清命令" if clear is None else acknowledged(clear)
        if self.plc is not None:
            self.log(f"PLC 清理：命令清零 {clear_text}；连接关闭 {acknowledged(report['plc_connection_closed'])}。", level)
        if self.agv_connected:
            result = report["agv"]
            self.log(f"AGV 清理：停止行走应答 {acknowledged(result.get('travel_stop_ack'))}；"
                     f"停止滚筒应答 {acknowledged(result.get('roller_stop_ack'))}；"
                     f"清目标应答 {acknowledged(result.get('target_clear_ack'))}。", level)
            self.log(f"AGV 行走停车反馈 {acknowledged(result.get('travel_stationary'))}；"
                     f"连接关闭 {acknowledged(result.get('connection_closed'))}。", level)
        self.log("清理后的滚筒及 PLC 机构停止状态需现场核对。" if self.plc is not None
                 else "滚筒机械停止状态需现场核对。", "WARNING")
        if report["errors"]:
            raise CleanupError("；".join(report["errors"]), report)
        return report
