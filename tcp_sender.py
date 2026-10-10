"""发送 HEX 对应的原始字节，验证 AGV TCP 接收脚本。示例数据不是 AGV 指令。"""

import argparse
import socket

from tcp_receiver import tcp_port


def hex_bytes(value):
    try:
        payload = bytes.fromhex(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("请输入完整的十六进制字节，例如 '01 02 AA BB'") from exc
    if not payload:
        raise argparse.ArgumentTypeError("报文不能为空")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="接收电脑的静态 IPv4 地址，默认 127.0.0.1")
    parser.add_argument("--port", type=tcp_port, default=1024, help="目标端口，默认 1024")
    parser.add_argument("--hex", type=hex_bytes, default="01 02 03 04 AA BB CC DD", help="待发送的 HEX 字节，带空格时请用引号包住")
    args = parser.parse_args(argv)
    try:
        with socket.create_connection((args.host, args.port), timeout=5) as client:
            client.sendall(args.hex)
    except OSError as exc:
        print(f"发送失败：{exc}。请检查接收端是否启动、目标 IP、端口和防火墙。", flush=True)
        return 1
    print(f"已向 {args.host}:{args.port} 发送 {len(args.hex)} 字节", flush=True)
    print(f"HEX: {args.hex.hex(' ').upper()}", flush=True)
    print("请在接收端窗口确认是否收到；发送成功不代表 AGV 已执行指令。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
