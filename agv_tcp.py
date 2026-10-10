"""按 AGV 调度说明生成 16 字节指令，经 TCP 发送并校验应答；仅使用标准库。"""

import argparse
import math
import socket
import time


AGV_HOST = "192.168.1.16"
AGV_PORT = 1024
FRAME_SIZE = 16
# 滚筒动作名称按《AGV远程调度说明(1).docx》，PDF 的上/下料名称存在差异。
COMMANDS = {
    "forward": (0x01, 0, "前进运行"),
    "backward": (0x01, 1, "后退运行"),
    "stop": (0x02, 0, "全部停止"),
    "slow-stop": (0x03, 0, "减速停止"),
    "clear-stations": (0x06, 0, "清除所有站点"),
    "roller-stop": (0x17, 0, "滚筒停止"),
    "roller-forward-send": (0x17, 1, "滚筒正转送料"),
    "roller-backward-send": (0x17, 2, "滚筒反转送料"),
    "roller-forward-receive": (0x17, 3, "滚筒正转接料"),
    "roller-backward-receive": (0x17, 4, "滚筒反转接料"),
}
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


def receive_reply(client, request, timeout, output=print):
    decoder = FrameDecoder()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("等待对应 AGV 应答超时")
        client.settimeout(remaining)
        data = client.recv(4096)
        if not data:
            raise ConnectionError("对端已断开，未收到对应的完整有效应答")
        output(f"接收数据块 HEX: {hex_text(data)}")
        before = decoder.discarded
        frames = decoder.feed(data)
        if decoder.discarded != before:
            output(f"已丢弃 {decoder.discarded - before} 字节噪声或无效报文")
        matched = None
        for frame in frames:
            output(f"有效应答帧 HEX: {hex_text(frame)}")
            if frame[1:3] == request[1:3] and matched is None:
                matched = frame
            elif frame[1:3] != request[1:3]:
                output("此帧 ID 或功能码不匹配，继续等待本次指令应答")
        if matched is not None:
            return matched


def print_status(status):
    print(f"AGV ID={status['agv_id']}，地标={status['landmark']}，当前站点={status['station']}")
    print(f"状态={status['state']}，速度={status['speed_percent']}%，挂钩={status['hook']}")
    print(f"电池电压={status['voltage']} V，报警 IO=0x{status['alarm_io']:02X}")
    print(f"有输入的 IO 编号={status['active_inputs']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help=f"目标 IP；默认本机测试，真实 AGV 文档地址为 {AGV_HOST}")
    parser.add_argument("--port", type=int, default=AGV_PORT, help="目标端口，默认 1024")
    parser.add_argument("--agv-id", type=int, default=1, help="AGV ID，1～255，默认 1")
    parser.add_argument("--command", required=True, choices=["station", "status", *COMMANDS], help="发送的指令；动作名称对照 Readme.md 的 AGV 协议与单指令测试章节")
    parser.add_argument("--station", type=int, help="目标站点 1～255，调度说明列出 1～10；仅用于 station")
    parser.add_argument("--query-data", type=int, choices=(0, 1), help="仅用于 status：默认 1（Word）；0 为 PDF 示例")
    parser.add_argument("--timeout", type=float, default=5, help="连接/等待应答超时秒数，默认 5")
    parser.add_argument("--send-only", action="store_true", help="只发送，不等待应答，用于仅打印 HEX 的 TCP 测试接收端")
    parser.add_argument("--dry-run", action="store_true", help="仅打印指令，不建立网络连接")
    args = parser.parse_args(argv)
    if not args.host.strip():
        parser.error("目标 IP 不能为空")
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为 1～65535")
    if not 1 <= args.agv_id <= 255:
        parser.error("AGV ID 必须为 1～255")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("超时必须为有限正数")
    if args.command == "station":
        if args.station is None or not 1 <= args.station <= 255:
            parser.error("station 指令需要 --station 1～255")
        function, data, label = 0x06, args.station, f"下发站点 {args.station}"
    elif args.command == "status":
        function, data, label = 0x05, 1 if args.query_data is None else args.query_data, "状态查询"
    else:
        function, data, label = COMMANDS[args.command]
    if args.command != "station" and args.station is not None:
        parser.error("--station 仅用于 station 指令")
    if args.command != "status" and args.query_data is not None:
        parser.error("--query-data 仅用于 status 指令")
    frame = build_frame(args.agv_id, function, data)
    print(f"目标 {args.host.strip()}:{args.port}，AGV ID={args.agv_id}，指令={label}", flush=True)
    print(f"发送帧（{len(frame)} 字节）HEX: {hex_text(frame)}", flush=True)
    if args.dry_run:
        print("预览完成，未建立连接")
        return 0
    sent = False
    try:
        with socket.create_connection((args.host.strip(), args.port), timeout=args.timeout) as client:
            client.sendall(frame)
            sent = True
            if args.send_only:
                print("已发送；请在接收电脑查看 HEX。本次未等待 AGV 应答。")
                return 0
            reply = receive_reply(client, frame, args.timeout, lambda message: print(message, flush=True))
            if function == 0x05:
                print_status(parse_status(reply))
            else:
                # Word 截图中下发站点2，应答数据为1；应答不是指令原样回显。
                print(f"已收到匹配 ID/功能码且校验正确的指令应答，数据码={int.from_bytes(reply[3:5], 'little')}")
                print("指令应答不表示动作完成，完成状态需通过状态查询或现场确认。")
    except KeyboardInterrupt:
        print("用户已中断")
        return 1
    except (OSError, ValueError) as exc:
        print(f"通讯失败：{exc}", flush=True)
        if sent:
            print("报文已发送，但本次未确认有效应答；程序不会自动重发。")
        else:
            print("未确认发送成功，请检查目标 IP、接收端、端口和防火墙。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
