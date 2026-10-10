"""显式的模拟反馈适配器；不创建 socket、PLC 客户端或真实连接。"""

import math
import time
from uuid import uuid4

from .models import EventKind, STAGES


SCENARIOS = (
    ("正常完整流程", "normal"),
    ("上料反馈异常", "loading_failure"),
    ("AGV 到站超时", "travel_failure"),
    ("退出清理异常", "cleanup_failure"),
)


class SimulationBackend:
    def __init__(self, parameters, settings, control, publish, time_scale=0.15, scenario="normal"):
        if not math.isfinite(time_scale) or time_scale <= 0:
            raise ValueError("模拟时间比例必须为有限正数。")
        if scenario not in {value for _, value in SCENARIOS}:
            raise ValueError("未知的模拟场景。")
        self.parameters, self.settings = parameters, settings
        self.control, self.publish = control, publish
        self.time_scale, self.scenario = time_scale, scenario
        self.loaded = False

    def log(self, text, level="INFO"):
        self.publish(EventKind.LOG, level=level, message=f"[模拟] {text}")

    def stage(self, index, detail):
        self.control.checkpoint()
        self.publish(EventKind.STAGE, stage=index, status="running", detail=detail)
        self.log(f"{STAGES[index]}：{detail}")

    def complete(self, index, detail):
        self.control.checkpoint()
        self.publish(EventKind.STAGE, stage=index, status="completed", detail=detail)

    def device(self, **values):
        self.control.checkpoint()
        self.publish(EventKind.DEVICE, **values)

    def delay(self, seconds):
        self.control.wait(seconds * self.time_scale)

    def confirm(self, key, title, message):
        # 跨任务也唯一，旧确认窗口不能误回答下一次任务的准备请求。
        request_id = uuid4().hex
        self.control.prepare_confirmation(request_id)
        self.log(f"等待人工确认：{title}", "WARNING")
        self.publish(EventKind.CONFIRMATION, request_id=request_id, key=key,
                     title=title, message=message, timeout=self.settings.transfer_timeout)
        self.control.wait_confirmation(request_id, self.settings.transfer_timeout)
        self.log(f"人工确认通过：{title}")

    def warehouse_move(self, index, column, level, label):
        self.stage(index, f"正在执行 A 库 {level} 层 {column} 列{label}")
        self.device(plc_state="空闲", plc_ready=True)
        self.log("收到模拟 PLC 空闲且就绪反馈，写入库位参数并下发动作")
        self.device(plc_state="忙碌")
        self.delay(2)
        self.device(plc_state="空闲")
        self.log("收到模拟 PLC 忙碌 → 空闲反馈，命令清零")
        self.complete(index, f"模拟{label}完成 · {column}-{level}")

    def require_material(self, expected):
        if self.loaded != expected:
            raise RuntimeError("模拟物料反馈不符，终止后续动作。")
        source = "物料 IO" if self.settings.material_input is not None else "物料交接"
        self.log(f"模拟{source}反馈检查通过：{'有料' if expected else '无料'}")

    def transfer(self, loading):
        index = 2 if loading else 5
        self.stage(index, "线体正在出货" if loading else "线体正在入货")
        self.require_material(not loading)
        self.device(plc_state="忙碌", state="停车等待上料" if loading else "停车等待下料")
        self.log(f"收到模拟线体忙碌反馈，本次线体时长 T={self.parameters.duration:g} 秒（演示加速）")
        if loading:
            self.delay(self.parameters.duration - 2)
        self.control.checkpoint()
        self.publish(EventKind.STAGE, stage=index, status="running",
                     detail="AGV 反转上料中" if loading else "AGV 正转下料中")
        self.device(state="反转上料" if loading else "正转下料")
        self.log("启动滚筒反转上料，最后 2 秒重叠" if loading else "启动滚筒正转下料，最初 2 秒重叠")
        self.delay(2)
        self.log("滚筒 2 秒计时结束，发送模拟停止；计时结束本身不证明交接完成")
        self.device(state="停站中", speed_percent=0)
        if not loading:
            self.delay(self.parameters.duration - 2)
        self.device(plc_state="空闲")
        self.log("线体命令清零，收到模拟空闲反馈")
        if loading and self.scenario == "loading_failure":
            raise RuntimeError("收到模拟上料未到位反馈，禁止 AGV 出发。")
        # 明确注入交接反馈，与计时完成分离；不是对真实货物的推断。
        self.loaded = loading
        self.require_material(loading)
        if loading and self.settings.material_input is None:
            self.publish(EventKind.STAGE, stage=index, status="running", detail="等待上料完成人工确认")
            self.confirm("loaded", "上料完成确认", "这是模拟确认。请确认模拟货物完全上车、滚筒停止且交接区已脱离，允许后退。")
        self.complete(index, "模拟上料条件已确认" if loading else "模拟下料反馈通过，线体空闲")

    def travel(self, index, target):
        direction = "后退前往站点 2" if target == 2 else "前进返回站点 1"
        self.stage(index, f"正在等待 AGV {direction}")
        self.device(target_station=target, state=direction, speed_percent=35, in_transit=True)
        self.delay(3)
        if target == 2 and self.scenario == "travel_failure":
            self.device(alarm="模拟到站超时")
            raise TimeoutError("模拟 AGV 到站超时。")
        # 显式生成两次到站反馈，模拟现有 wait_at_station 的稳定判断。
        for sample in range(2):
            self.device(raw_station=target, speed_percent=0, state="停站中")
            self.log(f"模拟到站反馈 {sample + 1}/2：站点 {target}、速度 0%、停站中")
            self.delay(self.settings.poll_interval)
        self.device(confirmed_station=target, target_station=None, in_transit=False)
        self.complete(index, f"站点 {target} 已确认（模拟）")

    def run(self):
        self.parameters.validate(self.settings)
        self.stage(0, "模拟设备检查，等待现场准备确认")
        self.device(plc_connection="模拟模式", agv_connection="模拟模式", plc_state="空闲",
                    plc_ready=True, confirmed_station=None, raw_station=1, target_station=None,
                    state="待准备确认", speed_percent=0, alarm="无（模拟）", voltage="24.50 V（模拟）",
                    in_transit=False)
        self.confirm("prepare", "现场准备确认", "这是模拟任务，不会操作设备。请确认源库位有货、AGV 空载、站点 1 可装卸，回库目标在入库时为空。")
        self.device(confirmed_station=1, state="停站中")
        self.complete(0, "模拟准备检查已确认")
        p = self.parameters
        self.warehouse_move(1, p.column, p.level, "出库")
        self.transfer(True)
        self.travel(3, 2)
        self.travel(4, 1)
        self.transfer(False)
        self.warehouse_move(6, p.inbound_column, p.inbound_level, "入库")

    def cleanup(self):
        # 清理不被取消中断；最长只演示 0.15 秒，不涉及真实设备。
        self.log("停止模拟行走和滚筒、清除模拟命令并释放资源")
        time.sleep(min(0.15, self.time_scale))
        self.publish(EventKind.DEVICE, plc_state="空闲", plc_ready=False,
                     state="模拟已停止", speed_percent=0, target_station=None, in_transit=False,
                     plc_connection="模拟模式", agv_connection="模拟模式")
        if self.scenario == "cleanup_failure":
            raise RuntimeError("演示清理反馈异常；请核对模拟日志。")
