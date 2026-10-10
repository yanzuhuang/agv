"""仓库 Modbus TCP 客户端：用户指定连接参数、库位及仓库/线体操作。

python warehouse_modbus.py                         # 按提示输入参数
python warehouse_modbus.py --operation inbound --column 4 --level 3
python warehouse_modbus.py --operation outbound --column 6 --level 1
python warehouse_modbus.py --operation line-inbound
python warehouse_modbus.py --operation line-outbound --duration 5
python warehouse_modbus.py --operation linked-inbound --column 4 --level 3
python warehouse_modbus.py --operation linked-outbound --column 6 --level 1 --duration 5

仓库操作限定 A 库，列号 1～6、层号 1～3。线体出货按指定秒数清零停止，
运行时长不代表物料已出货；请现场检查物料位置。PLC 须已回零、自动运行且就绪。
"""

import argparse
import math

from pymodbus.exceptions import ModbusException

from warehouse_plc import WarehouseError, WarehousePLC, log


PLC_HOST = "192.168.1.11"
PLC_PORT = 502
DEVICE_ID = 1
OPERATIONS = {
    "1": "outbound", "2": "line-outbound",
    "3": "line-inbound", "4": "inbound",
    "5": "linked-outbound", "6": "linked-inbound",
}
SLOT_OPERATIONS = ("inbound", "outbound", "linked-inbound", "linked-outbound")
OUTFEED_OPERATIONS = ("line-outbound", "linked-outbound")


def inbound(plc, column, level):
    """从皮带机取货并存入用户指定的 A 库库位。"""
    plc.move(4, column, level)


def outbound(plc, column, level):
    """从用户指定的 A 库库位取货并放到皮带机。"""
    plc.move(1, column, level)


def validate_slot(column, level):
    """联动开始前校验库位，避免先运行线体再发现目标参数错误。"""
    if type(column) is not int or not 1 <= column <= 6:
        raise ValueError("列号必须为 1～6")
    if type(level) is not int or not 1 <= level <= 3:
        raise ValueError("层号必须为 1～3")


def linked_inbound(plc, column, level):
    """线体入货完成后，货叉将物料存入指定库位。"""
    validate_slot(column, level)
    log("联动入库第 1 步：线体入货")
    plc.line_inbound()
    log("线体入货完成，联动入库第 2 步：仓库入库")
    inbound(plc, column, level)
    log("联动入库完成")


def linked_outbound(plc, column, level, duration):
    """仓库出库完成后，线体按用户指定时长出货并停止。"""
    validate_slot(column, level)
    if not math.isfinite(duration) or not 0 < duration <= plc.action_timeout:
        raise ValueError("线体出货运行秒数须为有限正数，且不超过动作超时")
    log("联动出库第 1 步：仓库出库")
    outbound(plc, column, level)
    log("仓库出库完成，联动出库第 2 步：线体出货")
    plc.line_outbound(duration)
    log("联动出库命令执行结束，请现场确认物料是否完全出货")


def prompt(label, convert=str, default=None, valid=lambda value: True):
    """收集并校验参数，输入错误时重输；所有输入都发生在连接 PLC 之前。"""
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


def prepare_args(parser, args):
    # 已提供的参数先校验，错误时不进入交互，也不建立连接。
    if args.plc_host is not None and not args.plc_host.strip():
        parser.error("PLC 地址不能为空")
    if args.plc_port is not None and not 1 <= args.plc_port <= 65535:
        parser.error("端口须为 1～65535")
    if args.device_id is not None and not 1 <= args.device_id <= 247:
        parser.error("Slave ID 须为 1～247")
    for name in ("ready_timeout", "start_timeout", "action_timeout"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"{name} 必须是有限的正数")
    if args.column is not None and not 1 <= args.column <= 6:
        parser.error("列号须为 1～6")
    if args.level is not None and not 1 <= args.level <= 3:
        parser.error("层号须为 1～3")
    if args.duration is not None and (not math.isfinite(args.duration) or not 0 < args.duration <= args.action_timeout):
        parser.error("--duration 必须是有限的正数，且不超过 --action-timeout")
    interactive = args.operation is None and not args.dry_run
    if interactive:
        args.plc_host = args.plc_host if args.plc_host is not None else prompt("PLC 地址", default=PLC_HOST, valid=bool)
        args.plc_port = args.plc_port if args.plc_port is not None else prompt("TCP 端口（1～65535）", int, PLC_PORT, lambda v: 1 <= v <= 65535)
        args.device_id = args.device_id if args.device_id is not None else prompt("Slave ID（1～247）", int, DEVICE_ID, lambda v: 1 <= v <= 247)
        print("选择操作：1 仓库出库 / 2 线体出货 / 3 线体入货 / 4 仓库入库")
        print("          5 联动出库（仓库出库 -> 线体出货）/ 6 联动入库（线体入货 -> 仓库入库）")
        selection = prompt("操作编号（1～6）", valid=lambda v: v in OPERATIONS)
        args.operation = OPERATIONS[selection]
    elif args.operation is None:
        parser.error("流程预览需要指定 --operation")

    args.plc_host = PLC_HOST if args.plc_host is None else args.plc_host.strip()
    args.plc_port = PLC_PORT if args.plc_port is None else args.plc_port
    args.device_id = DEVICE_ID if args.device_id is None else args.device_id
    if args.operation in SLOT_OPERATIONS:
        for name, maximum, label in (("column", 6, "列号（1～6）"), ("level", 3, "层号（1～3）")):
            value = getattr(args, name)
            if value is None:
                if args.dry_run:
                    parser.error("仓库及联动操作必须指定 --column 和 --level")
                value = prompt(label, int, valid=lambda v, maximum=maximum: 1 <= v <= maximum)
                setattr(args, name, value)
            if not 1 <= value <= maximum:
                parser.error(f"{label}超出范围")
    elif args.column is not None or args.level is not None:
        parser.error("独立线体操作不使用库位参数，请去掉 --column 和 --level")

    if args.operation in OUTFEED_OPERATIONS:
        if args.duration is None:
            if args.dry_run:
                parser.error("线体出货及联动出库必须指定 --duration")
            args.duration = prompt(
                f"线体出货运行秒数（大于 0，不超过 {args.action_timeout:g}）", float,
                valid=lambda v: math.isfinite(v) and 0 < v <= args.action_timeout,
            )
        if not math.isfinite(args.duration) or not 0 < args.duration <= args.action_timeout:
            parser.error("--duration 必须是有限的正数，且不超过 --action-timeout")
    elif args.duration is not None:
        parser.error("--duration 仅用于线体出货及联动出库")
    return args


def print_plan(args):
    print(f"PLC：{args.plc_host}:{args.plc_port}，Slave ID={args.device_id}")
    if args.operation in ("inbound", "outbound"):
        command = 4 if args.operation == "inbound" else 1
        label = "仓库入库" if command == 4 else "仓库出库"
        print(f"{label}：A 库 {args.level} 层 {args.column} 列 -> 命令 {command}")
        print("等待空闲且就绪 -> 写库位参数 -> 下发动作 -> 等待忙碌后空闲 -> 清 0")
    elif args.operation == "linked-inbound":
        print(f"联动入库：线体入货（命令 3）-> 完成并清 0 -> A 库 {args.level} 层 {args.column} 列入库（命令 4）-> 完成并清 0")
    elif args.operation == "linked-outbound":
        print(f"联动出库：A 库 {args.level} 层 {args.column} 列出库（命令 1）-> 完成并清 0 -> 线体出货（命令 2）")
        print(f"线体进入忙碌后最多运行 {args.duration:g} 秒，清 0 并等待空闲；物料是否完全出货需现场检查。")
    elif args.operation == "line-inbound":
        print("线体入货：命令 3 -> 等待忙碌 -> PLC 检测物料到位后空闲 -> 清 0")
    else:
        print(f"线体出货：命令 2 -> 等待忙碌 -> 运行 {args.duration:g} 秒 -> 清 0 -> 等待空闲")
        print("运行秒数用于停止皮带，物料是否完全出货需现场检查。")
    print("动作写入 40005（协议地址 4），40004 用于选择 A 库。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plc-host", "--host", default=None, help=f"PLC 地址，默认 {PLC_HOST}")
    parser.add_argument("--plc-port", "--port", type=int, default=None, help=f"TCP 端口，默认 {PLC_PORT}")
    parser.add_argument("--device-id", type=int, default=None, help="Slave ID，默认 1")
    parser.add_argument("--operation", choices=("inbound", "outbound", "line-inbound", "line-outbound", "linked-inbound", "linked-outbound"), help="不指定时进入操作菜单")
    parser.add_argument("--column", type=int, help="A 库列号：1～6")
    parser.add_argument("--level", type=int, help="A 库层号：1～3")
    parser.add_argument("--duration", type=float, help="线体出货/联动出库的线体运行秒数，到时清零停止")
    parser.add_argument("--dry-run", action="store_true", help="只预览指定流程，不连接 PLC")
    parser.add_argument("--ready-timeout", type=float, default=30, help="等待空闲就绪秒数")
    parser.add_argument("--start-timeout", type=float, default=15, help="等待进入忙碌秒数")
    parser.add_argument("--action-timeout", type=float, default=180, help="动作完成或停止超时秒数")
    try:
        args = prepare_args(parser, parser.parse_args(argv))
    except (EOFError, KeyboardInterrupt):
        log("参数输入取消，未连接 PLC")
        return 1
    print_plan(args)
    if args.dry_run:
        print("流程预览完成，未连接 PLC。")
        return 0

    plc = WarehousePLC(
        args.plc_host, args.plc_port, args.device_id,
        ready_timeout=args.ready_timeout, start_timeout=args.start_timeout,
        action_timeout=args.action_timeout,
    )
    result = 0
    try:
        log(f"连接 PLC {args.plc_host}:{args.plc_port}，Slave ID={args.device_id}")
        plc.connect()
        if args.operation == "inbound":
            inbound(plc, args.column, args.level)
        elif args.operation == "outbound":
            outbound(plc, args.column, args.level)
        elif args.operation == "line-inbound":
            plc.line_inbound()
        elif args.operation == "line-outbound":
            plc.line_outbound(args.duration)
        elif args.operation == "linked-inbound":
            linked_inbound(plc, args.column, args.level)
        elif args.operation == "linked-outbound":
            linked_outbound(plc, args.column, args.level, args.duration)
        log("所选操作执行结束")
    except KeyboardInterrupt:
        result = 1
        log("用户中断，本次操作终止")
    except (WarehouseError, ModbusException, OSError, ValueError) as exc:
        result = 1
        log(f"操作终止：{exc}")
    finally:
        if not plc.close():
            result = 1
    if result:
        log("请现场确认设备与物料状态；脚本不会自动重发或恢复任务。")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
