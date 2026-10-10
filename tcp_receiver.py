"""AGV TCP 收报测试：默认监听所有 IPv4 网卡的 1024 端口，打印原始字节的 HEX。"""

import argparse
from datetime import datetime
import socketserver
import threading


PRINT_LOCK = threading.Lock()


def log(message):
    with PRINT_LOCK:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def tcp_port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1～65535 之间")
    return port


class HexReceiver(socketserver.BaseRequestHandler):
    def handle(self):
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log(f"客户端已连接：{peer}")
        total = 0
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    break
                total += len(data)
                log(f"来源 {peer} | 收到 {len(data)} 字节 | HEX: {data.hex(' ').upper()}")
        except OSError as exc:
            log(f"连接异常 {peer}：{exc}")
        finally:
            log(f"客户端已断开：{peer}，累计收到 {total} 字节")


class TCPReceiverServer(socketserver.ThreadingTCPServer):
    # 每个连接独立处理；空闲连接不会阻止接收其他客户端或 Ctrl+C 退出。
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0；本机测试可用 127.0.0.1")
    parser.add_argument("--port", type=tcp_port, default=1024, help="监听端口，默认 1024")
    args = parser.parse_args(argv)
    try:
        with TCPReceiverServer((args.host, args.port), HexReceiver) as server:
            log(f"TCP Server 正在监听 {args.host}:{args.port}，按 Ctrl+C 停止")
            log("等待客户端发送数据；仅打印 HEX，不发送 AGV 协议应答。")
            server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("接收端已停止")
        return 0
    except OSError as exc:
        log(f"监听失败：{exc}。请检查监听 IP、端口占用和运行权限。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
