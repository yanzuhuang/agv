"""模拟设备与本地回环通信测试，不连接真实 AGV。"""

import contextlib
import io
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from outbound_agv import AGVClient, AGVError, Cancelled, deliver, main, parse_args, wait_at_station, wait_confirmation
from outbound_agv import build_frame, transfer
from outbound_agv import WarehouseOutbound


def status(station=1, state=0x0E, speed=0, material=False, alarm=0):
    return {"station": station, "state_code": state, "state": str(state),
            "speed_percent": speed, "active_inputs": [2] if material else [], "alarm_io": alarm}


class FakeAGV:
    def __init__(self, roller_profile="pdf"):
        self.load_code, self.unload_code = {"pdf": (2, 3), "word": (4, 1)}[roller_profile]
        self.commands = []
        self.station = 1
        self.material = False
        self.travelling = False
        self.fail_load = False
        self.target = 1
        self.direction = 0x0B

    def command(self, function, data, label):
        self.commands.append((function, data))
        if (function, data) == (0x17, self.load_code):
            if self.fail_load:
                raise TimeoutError("上料应答丢失")
            self.material = True
        elif (function, data) == (0x17, self.unload_code):
            self.material = False
        elif function == 0x06 and data != 0:
            self.target = data
        elif function == 0x01:
            self.travelling = True
            self.direction = 0x0B if data == 1 else 0x0A

    def status(self, timeout=None):
        if self.travelling:
            self.travelling = False
            previous = self.station
            self.station = self.target
            return status(previous, self.direction, 20, self.material)
        return status(self.station, material=self.material)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # 流程顺序测试跳过真实等待；精确计时由 TimingTests 的模拟时钟验证。
        def skip_wait(deadline, feeder=None, feed_deadline=None):
            if feeder is not None:
                feeder.stop_feed(wait=False)
        timer = patch("outbound_agv.wait_timed", side_effect=skip_wait)
        timer.start()
        self.addCleanup(timer.stop)

    def args(self, *extra):
        return parse_args(["--operation", "deliver", "--roller-profile", "pdf",
                           "--agv-only", "--poll-interval", "0.001", *extra])

    def test_sensor_delivery_command_order_and_direction(self):
        client = FakeAGV()
        args = self.args("--material-input", "2", "--loaded-level", "1")
        with patch("outbound_agv.wait_confirmation"), contextlib.redirect_stdout(io.StringIO()):
            deliver(client, args)
        self.assertEqual(client.commands, [(2, 0), (0x17, 0), (6, 0),
                         (0x17, 2), (0x17, 0), (6, 2), (1, 1), (2, 0),
                         (6, 0), (6, 1), (1, 0), (2, 0), (0x17, 3), (0x17, 0)])
        self.assertEqual(client.station, 1)
        self.assertFalse(client.material)

    def test_word_mapping_and_only_initial_confirmation(self):
        client = FakeAGV("word")
        args = self.args("--material-input", "2", "--loaded-level", "1")
        args.roller_profile = "word"
        with patch("outbound_agv.wait_confirmation") as confirm, contextlib.redirect_stdout(io.StringIO()):
            deliver(client, args)
        self.assertEqual(confirm.call_count, 1)
        self.assertEqual([item for item in client.commands if item[0] == 0x17],
                         [(0x17, 0), (0x17, 4), (0x17, 0), (0x17, 1), (0x17, 0)])
        self.assertFalse(client.material)

    def test_wrong_initial_station_never_starts_delivery(self):
        client = FakeAGV()
        client.station = 2
        with self.assertRaises(AGVError):
            deliver(client, self.args())
        self.assertEqual(client.commands, [])

    def test_loading_timeout_prevents_travel_without_resend(self):
        client = FakeAGV()
        client.fail_load = True
        with patch("outbound_agv.wait_confirmation"), self.assertRaises(TimeoutError):
            deliver(client, self.args())
        self.assertEqual(client.commands.count((0x17, 2)), 1)
        self.assertNotIn((1, 1), client.commands)

    def test_unverified_loading_cancel_prevents_target_and_travel(self):
        client = FakeAGV()
        def confirm(*args, **kwargs):
            if (0x17, 2) in client.commands:
                self.assertEqual(client.commands[-1], (0x17, 0))
                raise Cancelled("货物未上车")
        with patch("outbound_agv.wait_confirmation", side_effect=confirm), self.assertRaises(Cancelled):
            deliver(client, self.args())
        self.assertNotIn((6, 2), client.commands)
        self.assertNotIn((1, 1), client.commands)

    def test_unverified_loading_timeout_prevents_travel(self):
        client = FakeAGV()
        with patch("outbound_agv.wait_confirmation", side_effect=[None, AGVError("等待现场确认超时")]), self.assertRaises(AGVError):
            deliver(client, self.args())
        self.assertNotIn((1, 1), client.commands)

    def test_station_number_alone_does_not_mean_arrived(self):
        client = FakeAGV()
        args = self.args()
        with patch.object(client, "status", side_effect=[status(2, 0x0B, 20), status(2), status(2)]) as query:
            wait_at_station(client, 2, args)
        self.assertEqual(query.call_count, 3)

    def test_emergency_stop_aborts_travel(self):
        client = FakeAGV()
        with patch.object(client, "status", return_value=status(1, 6, alarm=4)), self.assertRaises(AGVError):
            wait_at_station(client, 2, self.args())

    def test_travel_timeout_prevents_unloading(self):
        client = FakeAGV()
        args = self.args("--travel-timeout", "0.003")
        with patch.object(client, "status", return_value=status(1, 0x0B, 20)), self.assertRaises(AGVError):
            wait_at_station(client, 2, args)
        self.assertEqual(client.commands, [])

    def test_load_requires_initial_empty_sensor(self):
        client = FakeAGV()
        client.material = True
        args = self.args("--material-input", "2", "--loaded-level", "1")
        with patch("outbound_agv.wait_confirmation"), self.assertRaises(AGVError):
            deliver(client, args)
        self.assertNotIn((0x17, 2), client.commands)

    def test_sensor_mismatch_after_loading_prevents_travel(self):
        client = FakeAGV()
        args = self.args("--material-input", "2", "--loaded-level", "1", "--transfer-timeout", "0.003")
        with patch.object(client, "status", return_value=status()), patch("outbound_agv.wait_confirmation"), self.assertRaises(AGVError):
            deliver(client, args)
        self.assertNotIn((1, 1), client.commands)

    def test_manual_cancel_and_timeout(self):
        import queue
        answers = queue.Queue()
        answers.put("q")
        with patch("outbound_agv.confirmation_reader", return_value=answers), self.assertRaises(Cancelled):
            wait_confirmation(FakeAGV(), 1, self.args(), "测试")
        with patch("outbound_agv.confirmation_reader", return_value=queue.Queue()), self.assertRaises(AGVError):
            wait_confirmation(FakeAGV(), 1, self.args("--transfer-timeout", "0.003"), "测试")

    def test_shutdown_runs_all_commands_even_after_failure(self):
        client = AGVClient()
        client.socket = object()
        calls = []

        def command(function, data, label):
            calls.append((function, data))
            if function == 2:
                raise TimeoutError("停止行走应答丢失")

        def reconnect():
            client.socket = object()
            client.broken = False

        with patch.object(client, "command", side_effect=command), patch.object(client, "connect", side_effect=reconnect), \
                patch.object(client, "close") as close, patch.object(client, "status", return_value=status()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(client.shutdown())
        self.assertEqual(calls, [(2, 0), (0x17, 0), (6, 0)])
        self.assertGreaterEqual(close.call_count, 1)

    def test_main_interrupt_always_shuts_down(self):
        with patch("outbound_agv.AGVClient") as constructor, patch("outbound_agv.deliver", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()):
            constructor.return_value.shutdown.return_value = True
            self.assertEqual(main(["--operation", "deliver", "--agv-only", "--roller-profile", "pdf"]), 1)
            constructor.return_value.shutdown.assert_called_once()

    def test_stop_mode_uses_shutdown_in_same_module(self):
        with patch("outbound_agv.AGVClient") as constructor, patch("outbound_agv.WarehouseOutbound") as warehouse, \
                contextlib.redirect_stdout(io.StringIO()):
            constructor.return_value.shutdown.return_value = True
            self.assertEqual(main(["--operation", "stop"]), 0)
            constructor.return_value.connect.assert_called_once()
            constructor.return_value.shutdown.assert_called_once()
            warehouse.assert_not_called()

    def test_single_file_copy_runs_without_project_modules(self):
        source = Path(__file__).with_name("outbound_agv.py")
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "outbound_agv.py"
            script.write_bytes(source.read_bytes())
            for arguments in (
                ["--column", "6", "--level", "1", "--roller-profile", "pdf", "--dry-run"],
                ["--operation", "stop", "--dry-run"],
                ["--operation", "exit"],
            ):
                with self.subTest(arguments=arguments):
                    result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(script), *arguments],
                                            cwd=folder, capture_output=True, text=True,
                                            encoding="utf-8", timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_args_dry_run_and_exit_never_connect(self):
        with patch("socket.create_connection") as connect, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for extra in (["--timeout", "nan"], ["--port", "0"], ["--material-input", "2"],
                          ["--loaded-level", "1"], ["--poll-interval", "0"],
                          ["--line-duration", "0.5"], ["--line-duration", "nan"]):
                with self.subTest(extra=extra), self.assertRaises(SystemExit) as error:
                    self.args(*extra)
                self.assertEqual(error.exception.code, 2)
            self.assertEqual(main(["--operation", "deliver", "--agv-only", "--roller-profile", "pdf", "--dry-run"]), 0)
            self.assertEqual(main(["--operation", "exit"]), 0)
            self.assertEqual(main(["--operation", "stop", "--dry-run"]), 0)
            connect.assert_not_called()

    def test_warehouse_outbound_and_belt_feed_precede_travel(self):
        events = []
        client = FakeAGV()
        warehouse = unittest.mock.Mock()
        warehouse.outbound.side_effect = lambda: events.append("outbound")
        warehouse.start_feed.side_effect = lambda: events.append("feed-on")
        warehouse.stop_feed.side_effect = lambda **kwargs: events.append("feed-off")
        original = client.command

        def command(function, data, label):
            events.append((function, data))
            return original(function, data, label)

        client.command = command
        args = self.args("--warehouse-outbound", "--column", "6", "--level", "1",
                         "--material-input", "2", "--loaded-level", "1")
        with patch("outbound_agv.wait_confirmation"), contextlib.redirect_stdout(io.StringIO()):
            deliver(client, args, warehouse)
        self.assertLess(events.index("outbound"), events.index((0x17, 2)))
        self.assertLess(events.index("feed-on"), events.index((0x17, 2)))
        self.assertLess(events.index("feed-on"), events.index("feed-off"))
        self.assertLess(events.index("feed-off"), events.index((1, 1)))
        self.assertIn((0x01, 0), client.commands)

    def test_warehouse_failure_prevents_loading(self):
        client = FakeAGV()
        warehouse = unittest.mock.Mock()
        warehouse.outbound.side_effect = AGVError("出库失败")
        with patch("outbound_agv.wait_confirmation"), self.assertRaises(AGVError):
            deliver(client, self.args(), warehouse)
        self.assertNotIn((0x17, 2), client.commands)
        self.assertNotIn((1, 1), client.commands)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class TimingTests(unittest.TestCase):
    def args(self, *extra):
        return parse_args(["--operation", "deliver", "--agv-only", "--roller-profile", "pdf", *extra])

    def run_timed_transfer(self, is_loading, line_duration=None, ack_delay=0):
        clock = FakeClock()
        client = FakeAGV()
        if not is_loading:
            client.material = True
        events = []
        command = client.command

        def record_command(function, data, label):
            events.append((clock.now, function, data))
            result = command(function, data, label)
            if function == 0x17 and data != 0:
                clock.sleep(ack_delay)
            return result

        client.command = record_command
        feeder = None
        args = self.args()
        if line_duration is not None:
            args.line_duration = line_duration
            feeder = unittest.mock.Mock()
            feeder.start_feed.side_effect = lambda: events.append((clock.now, "line", "start"))
            feeder.start_receive.side_effect = lambda: events.append((clock.now, "line", "receive"))
            feeder.stop_feed.side_effect = lambda **kwargs: events.append((clock.now, "line", "stop"))
        with patch("outbound_agv.time.monotonic", side_effect=clock.monotonic), \
                patch("outbound_agv.time.sleep", side_effect=clock.sleep), patch("outbound_agv.wait_confirmation"), contextlib.redirect_stdout(io.StringIO()):
            transfer(client, 1, is_loading, args, feeder)
        return events, feeder

    def test_line_five_seconds_load_starts_at_three_and_stops_at_five(self):
        events, feeder = self.run_timed_transfer(True, 5)
        self.assertEqual([(function, data) for _, function, data in events],
                         [("line", "start"), (0x17, 2), ("line", "stop"), (0x17, 0)])
        for event, expected in zip(events, [0, 3, 5, 5]):
            self.assertAlmostEqual(event[0], expected)
        feeder.stop_feed.assert_called_once_with(wait=False)
        feeder.wait_feed_stopped.assert_called_once()

    def test_custom_line_time_and_command_ack_do_not_extend_load(self):
        events, _ = self.run_timed_transfer(True, 8, ack_delay=0.2)
        for event, expected in zip(events, [0, 6, 8, 8]):
            self.assertAlmostEqual(event[0], expected)

    def test_agv_only_reverse_load_runs_two_seconds_including_ack(self):
        events, _ = self.run_timed_transfer(True, ack_delay=0.2)
        self.assertEqual([(function, data) for _, function, data in events],
                         [(0x17, 2), (0x17, 0)])
        self.assertAlmostEqual(events[1][0] - events[0][0], 2)

    def test_unload_at_station_one_runs_exactly_two_seconds(self):
        events, _ = self.run_timed_transfer(False, ack_delay=0.2)
        self.assertEqual([(function, data) for _, function, data in events], [(0x17, 3), (0x17, 0)])
        self.assertAlmostEqual(events[1][0] - events[0][0], 2)

    def test_short_line_duration_rejected(self):
        for duration in (1, 2, float('nan'), float('inf')):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.run_timed_transfer(True, duration)

    def test_unload_does_not_require_outfeed_duration(self):
        args = self.args()
        del args.line_duration
        client, feeder = FakeAGV(), Mock()
        client.material = True
        clock = FakeClock()
        with patch("outbound_agv.time", clock), contextlib.redirect_stdout(io.StringIO()):
            transfer(client, 1, False, args, feeder)
        self.assertAlmostEqual(clock.now, 2)
        feeder.finish_receive.assert_called_once()
        feeder.stop_feed.assert_not_called()

    def test_unload_only_times_rollers_and_waits_for_plc_completion(self):
        for duration in (5, 8, 2.1):
            with self.subTest(duration=duration):
                events, feeder = self.run_timed_transfer(False, duration, ack_delay=0.2)
                self.assertEqual([(f, d) for _, f, d in events],
                                 [("line", "receive"), (0x17, 3), (0x17, 0)])
                for event, expected in zip(events, [0, 0, 2]):
                    self.assertAlmostEqual(event[0], expected)
                feeder.start_feed.assert_not_called()
                feeder.stop_feed.assert_not_called()
                feeder.wait_feed_stopped.assert_not_called()
                feeder.finish_receive.assert_called_once()


class FakePLC:
    def __init__(self, states):
        self.states = iter(states)
        self.writes = []
        self.closed = False
        self.fail_action = False

    def read_holding_registers(self, address, *, count, device_id):
        state, ready = next(self.states)
        return unittest.mock.Mock(isError=lambda: False, registers=[state, 0, 0, 0, 0, ready])

    def write_register(self, address, value, *, device_id):
        self.writes.append((address, value))
        if self.fail_action and (address, value) == (4, 1):
            raise TimeoutError("写应答丢失")
        return unittest.mock.Mock(isError=lambda: False, address=address, registers=[value])

    def close(self):
        self.closed = True


class WarehouseTests(unittest.TestCase):
    def args(self):
        return parse_args(["--operation", "deliver", "--roller-profile", "pdf",
                           "--warehouse-outbound", "--column", "6", "--level", "1",
                           "--poll-interval", "0.001"])

    def test_outbound_then_feed_uses_only_commands_one_two_zero(self):
        plc = FakePLC([(0, 1), (0, 1), (1, 0), (0, 1), (0, 1), (1, 0), (1, 0), (0, 1)])
        warehouse = WarehouseOutbound(self.args(), plc)
        with contextlib.redirect_stdout(io.StringIO()):
            warehouse.outbound()
            warehouse.start_feed()
            warehouse.check_feed()
            warehouse.stop_feed()
            self.assertTrue(warehouse.close())
        self.assertEqual(plc.writes, [(4, 0), (1, 6), (2, 1), (3, 1), (4, 1), (4, 0),
                                     (4, 0), (4, 2), (4, 0)])
        self.assertTrue(plc.closed)

    def test_lost_write_reply_still_clears_without_resending(self):
        plc = FakePLC([(0, 1), (0, 1)])
        plc.fail_action = True
        warehouse = WarehouseOutbound(self.args(), plc)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):
                warehouse.outbound()
            self.assertTrue(warehouse.close())
        self.assertEqual(plc.writes.count((4, 1)), 1)
        self.assertEqual(plc.writes[-1], (4, 0))
        self.assertTrue(plc.closed)

    def test_standalone_receive_waits_busy_then_idle_before_clear(self):
        plc = FakePLC([(0, 1), (1, 0), (1, 0), (0, 1)])
        warehouse = WarehouseOutbound(self.args(), plc)
        with contextlib.redirect_stdout(io.StringIO()):
            warehouse.start_receive()
            self.assertEqual(plc.writes[-1], (4, 3))
            self.assertTrue(warehouse.active)
            warehouse.finish_receive()
        self.assertEqual(plc.writes, [(4, 0), (4, 3), (4, 0)])
        self.assertFalse(warehouse.active)

    def test_clear_failure_still_closes_connection(self):
        plc = FakePLC([])
        warehouse = WarehouseOutbound(self.args(), plc)
        warehouse.active = True
        with patch.object(warehouse, "clear", side_effect=TimeoutError), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(warehouse.close())
        self.assertTrue(plc.closed)


class LoopbackTests(unittest.TestCase):
    def test_persistent_connection_fragmentation_and_shutdown(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        listener.settimeout(3)
        received, errors = [], []

        def serve():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(3)
                        for _ in range(5):
                            frame = bytearray()
                            while len(frame) < 16:
                                chunk = connection.recv(16 - len(frame))
                                if not chunk:
                                    raise AssertionError("不完整请求")
                                frame.extend(chunk)
                            received.append(bytes(frame))
                            if frame[2] == 5:
                                body = bytes.fromhex("AA 01 05 00 01 01 00 0E 02 16 20 00 00 00 FF")
                                reply = body + bytes((sum(body) % 256,))
                            else:
                                reply = build_frame(1, frame[2], 1)
                            connection.sendall(b"noise" + build_frame(2, frame[2], 1) + reply[:6])
                            connection.sendall(reply[6:])
                        self.assertEqual(connection.recv(16), b"")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        client = AGVClient("127.0.0.1", port, timeout=2)
        try:
            client.connect()
            with contextlib.redirect_stdout(io.StringIO()):
                client.command(6, 2, "测试目标站点")
                self.assertTrue(client.shutdown())
            self.assertIsNone(client.socket)
        finally:
            client.close()
            worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual([(frame[2], frame[3]) for frame in received],
                         [(6, 2), (2, 0), (0x17, 0), (6, 0), (5, 1)])


if __name__ == "__main__":
    unittest.main()
