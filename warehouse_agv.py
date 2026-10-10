"""仓储出库、线体输送、AGV 往返搬运与回库的统一入口。

python warehouse_agv.py
python warehouse_agv.py --column 6 --level 1 --duration 5 --roller-profile word --inbound-column 6 --inbound-level 1
python warehouse_agv.py --dry-run --column 6 --level 1 --duration 5 --roller-profile word --inbound-column 6 --inbound-level 1
python warehouse_agv.py --operation stop

复用 warehouse_modbus.py（依赖 warehouse_plc.py）和 outbound_agv.py。
上料：线体最后 2 秒与 AGV 滚筒同时运行；下料：最初 2 秒同时运行。
上下料线体共用输入的运行时间，线体停止后存入指定 A 库库位。
"""

import argparse
import math

import outbound_agv as agv


def prompt(label, convert=str, default=None, valid=lambda value: True):
    """连接设备前收集参数，输入无效时重新输入。"""
    while True:
        hint = f" [{default}]" if default is not None else ""
        raw = input(f"{label}{hint}：").strip()
        try:
            value = default if not raw and default is not None else convert(raw)
            if valid(value):
                return value
        except (TypeError, ValueError):
            pass
        print("输入无效，请按提示重新输入。", flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--operation", choices=("deliver", "stop", "exit"), default="deliver",
                        help="默认完整搬运；stop 仅停止 AGV；exit 不连接设备")
    parser.add_argument("--plc-host")
    parser.add_argument("--plc-port", type=int)
    parser.add_argument("--device-id", type=int)
    parser.add_argument("--column", type=int)
    parser.add_argument("--level", type=int)
    parser.add_argument("--inbound-column", type=int, help="回库目标列号（1～6）")
    parser.add_argument("--inbound-level", type=int, help="回库目标层号（1～3）")
    parser.add_argument("--duration", "--line-duration", dest="duration", type=float,
                        help="上下料共用的线体运行秒数，须大于2秒")
    parser.add_argument("--host", "--agv-host", default=agv.AGV_HOST)
    parser.add_argument("--port", "--agv-port", type=int, default=agv.AGV_PORT)
    parser.add_argument("--agv-id", type=int, default=1)
    parser.add_argument("--roller-profile", choices=agv.ROLLER_PROFILES)
    parser.add_argument("--material-input", type=int)
    parser.add_argument("--loaded-level", type=int, choices=(0, 1))
    parser.add_argument("--query-data", type=int, choices=(0, 1), default=1)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--ready-timeout", type=float, default=30)
    parser.add_argument("--start-timeout", type=float, default=15)
    parser.add_argument("--action-timeout", type=float, default=180)
    parser.add_argument("--travel-timeout", type=float, default=180)
    parser.add_argument("--transfer-timeout", type=float, default=120)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不连接任何设备")
    args = parser.parse_args(argv)

    # 所有已有参数先校验，避免输入后才报错，更不允许出货后才发现参数错误。
    for name in ("timeout", "ready_timeout", "start_timeout", "action_timeout",
                 "travel_timeout", "transfer_timeout", "poll_interval"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name} 必须为有限正数")
    for name, maximum in (("port", 65535), ("agv_id", 255), ("plc_port", 65535),
                          ("device_id", 247), ("column", 6), ("level", 3),
                          ("inbound_column", 6), ("inbound_level", 3),
                          ("material_input", 8)):
        value = getattr(args, name)
        if value is not None and not 1 <= value <= maximum:
            parser.error(f"{name} 必须为 1～{maximum}")
    for name in ("host", "plc_host"):
        value = getattr(args, name)
        if value is not None:
            if not value.strip():
                parser.error(f"{name} 不能为空")
            setattr(args, name, value.strip())
    if (args.material_input is None) != (args.loaded_level is None):
        parser.error("--material-input 与 --loaded-level 必须一起提供")
    if args.duration is not None and (
            not math.isfinite(args.duration) or not agv.LOAD_SECONDS < args.duration <= args.action_timeout):
        parser.error("duration 必须为大于2秒的有限数，且不超过 action-timeout")
    if args.operation != "deliver":
        return args

    fields = (
        ("plc_host", "PLC 地址", str, "192.168.1.11", bool),
        ("plc_port", "PLC TCP 端口", int, 502, lambda v: 1 <= v <= 65535),
        ("device_id", "PLC Slave ID", int, 1, lambda v: 1 <= v <= 247),
        ("column", "A 库出库列号（1～6）", int, None, lambda v: 1 <= v <= 6),
        ("level", "A 库出库层号（1～3）", int, None, lambda v: 1 <= v <= 3),
        ("duration", f"上下料线体运行秒数（大于 2，不超过 {args.action_timeout:g}）",
         float, None, lambda v: math.isfinite(v) and agv.LOAD_SECONDS < v <= args.action_timeout),
        ("roller_profile", "滚筒定义：word 反转上料04/下料01；pdf 反转上料02/下料03",
         str, None, lambda v: v in agv.ROLLER_PROFILES),
        ("inbound_column", "A 库入库目标列号（1～6，可填原出库列号）",
         int, None, lambda v: 1 <= v <= 6),
        ("inbound_level", "A 库入库目标层号（1～3，可填原出库层号）",
         int, None, lambda v: 1 <= v <= 3),
    )
    if args.dry_run:
        for name, _, _, default, _ in fields:
            if getattr(args, name) is None:
                if default is None:
                    parser.error(f"预览必须提供 --{name.replace('_', '-')}")
                setattr(args, name, default)
    else:
        for name, label, convert, default, valid in fields:
            if getattr(args, name) is None:
                setattr(args, name, prompt(label, convert, default, valid))
    args.line_duration = args.duration
    return args


def print_plan(args):
    if args.operation == "exit":
        agv.log("直接退出，不连接设备。")
        return
    agv.log(f"AGV：{args.host}:{args.port}，ID={args.agv_id}")
    if args.operation == "stop":
        agv.log("停止 AGV 行走、停止滚筒、清除目标站点并退出；此操作不控制仓储 PLC。")
        return
    agv.log(f"PLC：{args.plc_host}:{args.plc_port}，Slave ID={args.device_id}")
    agv.log(f"A 库 {args.level} 层 {args.column} 列出货 -> 线体出货 {args.duration:g} 秒，最后2秒AGV上料，两者到时停止")
    agv.log(f"站点 1 上料 {agv.LOAD_SECONDS:g} 秒 -> 后退到站点 2 -> 前进到站点 1"
            f" -> 下料 {agv.UNLOAD_SECONDS:g} 秒")
    agv.log(f"下料时线体入货（命令3）与滚筒同时启动，2秒停滚筒，{args.duration:g}秒停线体并确认空闲"
            f" -> A 库 {args.inbound_level} 层 {args.inbound_column} 列入库（命令 4）")
    load, unload = agv.ROLLER_PROFILES[args.roller_profile]
    agv.log(f"滚筒定义 {args.roller_profile}：上料 0x{load:02X}，下料 0x{unload:02X}。")
    agv.log("AGV 起点须已现场确认为站点 1；启动上料阶段允许反馈 0 或 1，保留准备确认。")


class Conveyor:
    """复用现有 PLC 连接与清理责任，供共用装卸时序调度。"""

    def __init__(self, plc):
        self.plc = plc

    def start_feed(self):
        self.plc.begin(2)
        self.plc.wait_started()

    def start_receive(self):
        self.plc.begin(3)
        self.plc.wait_started()

    def stop_feed(self, wait=True):
        self.plc.clear_command()
        if wait:
            self.wait_feed_stopped()

    def wait_feed_stopped(self):
        self.plc.wait_status(0, self.plc.action_timeout, "等待装卸线体停止")


def deliver(client, plc, args, warehouse, *, hooks=None):
    """仓库出库、重叠装卸、往返与回库；上下料共用线体时间。"""
    if hooks is not None:
        with agv.execution_hooks(hooks):
            return deliver(client, plc, args, warehouse)
    # 即使直接调用本函数，也在出货前拒绝无效的回库目标。
    if (type(args.inbound_column) is not int or not 1 <= args.inbound_column <= 6
            or type(args.inbound_level) is not int or not 1 <= args.inbound_level <= 3):
        raise ValueError("入库目标须为 A 库 1～6 列、1～3 层")
    if not math.isfinite(args.duration) or not agv.LOAD_SECONDS < args.duration <= args.action_timeout:
        raise ValueError("线体运行时间须大于2秒且不超过动作超时")
    args.line_duration = args.duration
    feeder = Conveyor(plc)
    agv.stage_event(0, "running", "检查起点、空载和现场准备条件")
    initial = client.status()
    agv.require_stationary(initial, 1, allow_initial_zero=True)
    if initial["station"] == 0:
        agv.log('初始站点反馈=0，按现场已确认的站点1起点处理；仍检查停车和报警。')
    if args.material_input is not None and agv.loaded(initial, args):
        raise agv.AGVError("AGV 已有料，不能开始出库取货任务")
    client.command(0x02, 0, "站点 1：停止行走")
    client.command(0x17, 0, "站点 1：停止滚筒")
    client.command(0x06, 0, "清除之前的目标站点")
    agv.wait_confirmation(client, 1, args,
                          "确认源库位有货、AGV 空载，站点 1 可上料并接收返回后的下料；"
                          "入库目标在回库时须为空（可使用本次腾空的源库位）",
                          allow_initial_zero=True)

    agv.stage_event(0, "completed", "现场准备已确认；保留原始站点反馈")
    agv.stage_event(1, "running", "A 库出库：等待忙碌后恢复空闲")
    warehouse.outbound(plc, args.column, args.level)
    agv.stage_event(1, "completed", "出库完成且命令已清零")
    agv.stage_event(2, "running", "线体最后 2 秒与 AGV 上料重叠")
    agv.transfer(client, 1, True, args, feeder, allow_initial_zero=True)
    agv.stage_event(2, "completed", "上料停止且物料交接已确认")
    agv.stage_event(3, "running", "后退到站点 2，需连续两次停车反馈")
    client.command(0x06, 2, "设置目标站点 2")
    client.command(0x01, 1, "从站点 1 后退到站点 2")
    agv.wait_at_station(client, 2, args)
    client.command(0x02, 0, "站点 2：停止行走")
    agv.require_stationary(client.status(), 2)
    agv.stage_event(3, "completed", "站点 2 到站停车已确认")
    agv.stage_event(4, "running", "前进返回站点 1，需连续两次停车反馈")
    client.command(0x06, 0, "清除旧目标站点")
    client.command(0x06, 1, "设置返回目标站点 1")
    client.command(0x01, 0, "从站点 2 前进到站点 1")
    agv.wait_at_station(client, 1, args, direction="前进")
    client.command(0x02, 0, "返回站点 1：停止行走")
    agv.require_stationary(client.status(), 1)
    agv.stage_event(4, "completed", "站点 1 到站停车已确认")
    agv.stage_event(5, "running", "线体最初 2 秒与 AGV 下料重叠")
    agv.transfer(client, 1, False, args, feeder)
    agv.stage_event(5, "completed", "下料定时结束，线体空闲已确认")
    agv.log("AGV 下料及线体入货结束，开始仓储入库。")
    agv.stage_event(6, "running", "A 库入库：等待忙碌后恢复空闲")
    warehouse.inbound(plc, args.inbound_column, args.inbound_level)
    agv.stage_event(6, "completed", "入库完成且命令已清零")
    agv.log("仓储出货、AGV 往返搬运、线体入货及仓储入库流程结束。")


def main(argv=None):
    try:
        args = parse_args(argv)
    except (EOFError, KeyboardInterrupt):
        agv.log("参数输入取消，未连接设备。")
        return 1
    print_plan(args)
    if args.dry_run or args.operation == "exit":
        return 0

    client = agv.AGVClient(args.host, args.port, args.agv_id, args.timeout, args.query_data)
    plc = None
    connected = False
    result = 0
    try:
        if args.operation == "deliver":
            # 提前加载依赖；依赖缺失时，尚未连接或启动任何设备。
            import warehouse_modbus as warehouse
            plc = warehouse.WarehousePLC(
                args.plc_host, args.plc_port, args.device_id,
                ready_timeout=args.ready_timeout, start_timeout=args.start_timeout,
                action_timeout=args.action_timeout,
            )
        client.connect()
        connected = True
        if plc is not None:
            plc.connect()
            deliver(client, plc, args, warehouse)
        # stop 的三条停止指令由 finally 内 shutdown 统一执行。
    except (Exception, KeyboardInterrupt) as exc:
        result = 1
        agv.log(f"任务终止：{type(exc).__name__}: {exc}；不会自动重发搬运指令。")
        if isinstance(exc, ModuleNotFoundError) and exc.name == "warehouse_plc":
            agv.log("请将真机已验证的 warehouse_plc.py 放回本目录；未替换原仓储驱动。")
    finally:
        try:
            if plc is not None and not plc.close():
                result = 1
        except (Exception, KeyboardInterrupt) as exc:
            result = 1
            agv.log(f"仓储退出清理未确认：{exc}")
        finally:
            if connected:
                if not client.shutdown():
                    result = 1
            else:
                client.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
