"""协议样例、流式拆包和本地 TCP 应答测试；不会连接真实 AGV。"""

import contextlib
import io
import socket
import threading
import unittest
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from agv_tcp import (
    COMMANDS, FrameDecoder, build_frame, main, parse_status, receive_reply,
    validate_frame,
)


class ProtocolTests(unittest.TestCase):
    def test_all_word_command_examples_match_generated_bytes(self):
        # 直接读用户提供的Word表格作对照，涵盖10个站点、动作及状态查询。
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with ZipFile(next(Path(__file__).parent.glob("AGV*.docx"))) as document:
            root = ET.fromstring(document.read("word/document.xml"))
        samples = []
        for row in root.findall(".//w:tr", ns):
            cells = row.findall("w:tc", ns)
            if len(cells) != 3:
                continue
            text = "".join(t.text or "" for t in cells[2].findall(".//w:t", ns)).strip()
            if text.startswith("AA "):
                samples.append(bytes.fromhex(text))
        self.assertEqual(len(samples), 18)
        expected = [build_frame(1, 0x06, station) for station in range(1, 11)]
        expected += [build_frame(1, *COMMANDS[name][:2]) for name in (
            "forward", "backward", "roller-forward-send", "roller-backward-send",
            "roller-forward-receive", "roller-backward-receive", "stop",
        )]
        expected.append(build_frame(1, 0x05, 1))
        self.assertEqual(samples, expected)

    def test_stream_fragmentation_noise_bad_checksum_and_coalescing(self):
        first = build_frame(1, 0x06, 1)
        second = build_frame(1, 0x01, 1)
        corrupt = first[:-1] + bytes((first[-1] ^ 1,))
        stream = b"noise" + corrupt + first + second
        decoder = FrameDecoder()
        decoded = []
        # 一个字节一次到达，包括帧头、帧尾和校验被拆开的情况。
        for byte in stream:
            decoded.extend(decoder.feed(bytes((byte,))))
        self.assertEqual(decoded, [first, second])
        self.assertEqual(decoder.discarded, 21)
        self.assertEqual(FrameDecoder().feed(first + second), [first, second])

    def test_status_fields_against_pdf_layout_and_voltage_example(self):
        body = bytes.fromhex("AA 01 05 01 02 02 14 0E 02 16 20 05 21 00 FF")
        frame = body + bytes((sum(body) % 256,))
        status = parse_status(frame)
        self.assertEqual(status["landmark"], 258)
        self.assertEqual(status["station"], 2)
        self.assertEqual(status["speed_percent"], 20)
        self.assertEqual(status["state"], "停站中")
        self.assertEqual(status["hook"], "降")
        self.assertEqual(status["voltage"], "22.32")
        self.assertEqual(status["alarm_io"], 5)
        self.assertEqual(status["active_inputs"], [1, 6])
        with self.assertRaises(ValueError):
            validate_frame(frame[:-1] + b"\x00")
        with self.assertRaises(ValueError):
            parse_status(build_frame(1, 0x06, 1))

    def test_invalid_arguments_and_dry_run_never_connect(self):
        for args in (
            ["--command", "station"],
            ["--command", "station", "--station", "0"],
            ["--command", "forward", "--station", "2"],
            ["--command", "status", "--timeout", "nan"],
            ["--command", "stop", "--agv-id", "0"],
            ["--command", "stop", "--port", "65536"],
            ["--command", "stop", "--query-data", "1"],
        ):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--command", "station", "--station", "2", "--dry-run"]), 0)
        self.assertIn("AA 01 06 02 00 00 00 00 00 00 00 00 00 00 FF B2", output.getvalue())


class NetworkTests(unittest.TestCase):
    def start_peer(self, handler):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        errors = []

        def serve():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(3)
                        received = bytearray()
                        while len(received) < 16:
                            chunk = connection.recv(16 - len(received))
                            if not chunk:
                                raise AssertionError("Incomplete request")
                            received.extend(chunk)
                        handler(connection, bytes(received))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return listener.getsockname()[1], thread, errors

    def finish_peer(self, thread, errors):
        thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    def test_station_reply_data_is_one_not_echoed_station_two(self):
        request = build_frame(1, 0x06, 2)
        reply = build_frame(1, 0x06, 1)  # Word 截图的真实应答示例。

        def handler(connection, received):
            self.assertEqual(received, request)
            connection.sendall(build_frame(2, 0x06, 1) + build_frame(1, 0x01, 1) + reply[:7])
            connection.sendall(reply[7:])

        port, thread, errors = self.start_peer(handler)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                client.sendall(request)
                self.assertEqual(receive_reply(client, request, 2, lambda message: None), reply)
        finally:
            self.finish_peer(thread, errors)

    def test_timeout_does_not_resend(self):
        request = build_frame(1, 0x06, 2)

        def handler(connection, received):
            self.assertEqual(received, request)
            # 超时后发送端关闭，服务端必须读到EOF，没有第二次请求。
            self.assertEqual(connection.recv(4096), b"")

        port, thread, errors = self.start_peer(handler)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(["--host", "127.0.0.1", "--port", str(port),
                               "--command", "station", "--station", "2", "--timeout", "0.1"])
            self.assertEqual(result, 1)
            self.assertIn("不会自动重发", output.getvalue())
        finally:
            self.finish_peer(thread, errors)

    def test_status_query_uses_pdf_alternative_and_parses_response(self):
        body = bytes.fromhex("AA 01 05 01 02 02 14 0E 02 16 20 05 21 00 FF")
        reply = body + bytes((sum(body) % 256,))

        def handler(connection, received):
            self.assertEqual(received, build_frame(1, 0x05, 0))
            connection.sendall(reply)

        port, thread, errors = self.start_peer(handler)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(["--host", "127.0.0.1", "--port", str(port),
                               "--command", "status", "--query-data", "0"])
            self.assertEqual(result, 0)
            self.assertIn("电池电压=22.32 V", output.getvalue())
            self.assertIn("地标=258", output.getvalue())
        finally:
            self.finish_peer(thread, errors)


if __name__ == "__main__":
    unittest.main()
