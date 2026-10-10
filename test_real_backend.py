"""真实执行路径的离线测试：假 AGV + 真 PLC 驱动/内存 Modbus，不连接现场。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from functools import partial
import queue
import threading
import unittest
from unittest.mock import Mock, patch

import outbound_agv as agv
import warehouse_plc
from gui.models import EventKind, TaskParameters
from gui.real_backend import CleanupError, RealBackend
from gui.settings import DeviceSettings
from gui.task_worker import TaskCancelled, TaskControl, TaskWorker
from test_agv_task import FakeAGV, FakeClock


class MemoryModbus:
    def __init__(self):
        self.command = 0
        self.busy_seen = False
        self.writes = []
        self.closed = False
        self.fail_connect = False
        self.fail_clear = False

    def connect(self):
        return not self.fail_connect

    def close(self):
        self.closed = True

    def read_holding_registers(self, address, **kwargs):
        value = 1
        if address == 0:
            if self.command in (1, 4):
                value = 0 if self.busy_seen else 1
                self.busy_seen = True
            else:
                value = int(self.command in (2, 3))
        return Mock(registers=[value], isError=lambda: False)

    def write_register(self, address, value, **kwargs):
        self.writes.append((address, value, agv.time.monotonic()))
        if address == 4:
            if value == 0 and self.fail_clear:
                raise TimeoutError("PLC 清零响应丢失")
            self.command = value
            self.busy_seen = False
        return Mock(isError=lambda: False)


class ConnectedFakeAGV(FakeAGV):
    shutdown = agv.AGVClient.shutdown

    def __init__(self, *args, profile="pdf"):
        super().__init__(profile)
        self.status_observer = None
        self.socket = None
        self.broken = False
        self.closed = False
        self.fail_connect = False
        self.fail_stop = False
        self.reads = 0
        self.on_status = self.on_command = None
        self.timed_commands = []
        self.command_threads = []

    def connect(self):
        if self.fail_connect:
            raise ConnectionError("AGV 离线")
        self.socket = object()
        self.broken = False

    def close(self):
        self.closed = True
        self.socket = None

    def command(self, function, data, label):
        agv.checkpoint()
        self.command_threads.append(threading.get_ident())
        self.timed_commands.append((function, data, agv.time.monotonic()))
        if self.fail_stop and function == 2:
            self.commands.append((function, data))
            raise TimeoutError("停止行走响应丢失")
        try:
            result = super().command(function, data, label)
        except TimeoutError:
            self.broken = True
            raise
        if function == 2:
            self.travelling = False
        if self.on_command:
            self.on_command(function, data)
        return result

    def status(self, timeout=None):
        agv.checkpoint()
        self.reads += 1
        result = super().status(timeout)
        result["state"] = agv.STATE_NAMES[result["state_code"]]
        result["voltage"] = "24.50"
        if self.on_status:
            self.on_status(result)
        if self.status_observer:
            self.status_observer(dict(result))
        return result


class RealBackendTests(unittest.TestCase):
    def setUp(self):
        self.settings = DeviceSettings(roller_profile="pdf", material_input=2, loaded_level=1,
                                       poll_interval=0.001, plc_poll_interval=0.001)
        self.parameters = TaskParameters(6, 1, 4, 3, 5)
        self.control = TaskControl()
        self.events = []
        self.client = ConnectedFakeAGV()
        self.bus = MemoryModbus()
        self.plc = None
        self.auto_answer = True
        self.clock = FakeClock()
        for target in ("socket.create_connection", "socket.socket", "builtins.input",
                       "pymodbus.client.ModbusTcpClient.connect"):
            guard = patch(target, side_effect=AssertionError(f"禁止真实通信：{target}"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)

    def publish(self, kind, **data):
        self.events.append((kind, data))
        if kind == EventKind.CONFIRMATION and self.auto_answer:
            self.control.answer(data["request_id"], True)

    def plc_factory(self, *args, **kwargs):
        self.plc = warehouse_plc.WarehousePLC(*args, **kwargs, client=self.bus)
        return self.plc

    def backend(self, settings=None):
        return RealBackend(self.parameters, settings or self.settings, self.control, self.publish,
                           agv_factory=lambda *a: self.client, plc_factory=self.plc_factory)

    def run_fast(self, backend):
        # 仅替换控制模块的时钟对象，不修改全局 time 或跳过装卸调度。
        with patch.object(agv, "time", self.clock), patch.object(warehouse_plc, "time", self.clock):
            backend.run()
            return backend.cleanup()

    def test_complete_flow_uses_original_commands_and_real_seconds(self):
        report = self.run_fast(self.backend())
        self.assertEqual(self.client.commands, [(2, 0), (23, 0), (6, 0),
                         (23, 2), (23, 0), (6, 2), (1, 1), (2, 0),
                         (6, 0), (6, 1), (1, 0), (2, 0), (23, 3), (23, 0),
                         (2, 0), (23, 0), (6, 0)])
        starts = [(value, stamp) for address, value, stamp in self.bus.writes if address == 4 and value]
        self.assertEqual([v for v, t in starts], [1, 2, 3, 4])
        load = next(t for f, d, t in self.client.timed_commands if (f, d) == (23, 2))
        unload = next(t for f, d, t in self.client.timed_commands if (f, d) == (23, 3))
        self.assertAlmostEqual(load - starts[1][1], 3)
        self.assertAlmostEqual(unload - starts[2][1], 0)
        roller_stops = [t for f, d, t in self.client.timed_commands if (f, d) == (23, 0)]
        self.assertAlmostEqual(roller_stops[1] - load, 2)
        self.assertAlmostEqual(roller_stops[2] - unload, 2)
        for (command, start), next_action in zip(starts[1:3], (starts[2][1], starts[3][1])):
            cleared = next(t for a, v, t in self.bus.writes if a == 4 and v == 0 and t >= start + 5)
            self.assertAlmostEqual(cleared - start, 5)
            self.assertGreaterEqual(next_action, cleared)
        completed = [d["stage"] for k, d in self.events if k == EventKind.STAGE and d["status"] == "completed"]
        self.assertEqual(completed, list(range(7)))
        self.assertTrue(report["agv"]["travel_stationary"])
        self.assertTrue(report["agv"]["roller_stop_ack"])
        self.assertIsNone(report["agv"]["roller_stationary"])
        self.assertIsNone(report["plc_mechanical_stop"])
        self.assertTrue(self.client.closed and self.bus.closed)

    def test_without_material_io_requires_two_gui_confirmations(self):
        self.run_fast(self.backend(replace(self.settings, material_input=None, loaded_level=None)))
        self.assertEqual([d["key"] for k, d in self.events if k == EventKind.CONFIRMATION], ["prepare", "loaded"])

    def test_waiting_confirmation_keeps_polling_and_alarm_aborts_before_outbound(self):
        self.auto_answer = False
        def alarm(status):
            if self.client.reads >= 3:
                status["alarm_io"] = 4
        self.client.on_status = alarm
        backend = self.backend()
        with self.assertRaises(agv.AGVError):
            backend.run()
        self.client.on_status = None
        backend.cleanup()
        self.assertGreaterEqual(self.client.reads, 3)
        self.assertFalse(any(a == 4 and v for a, v, t in self.bus.writes))

    def test_confirmation_timeout_prevents_outbound(self):
        self.auto_answer = False
        backend = self.backend(replace(self.settings, transfer_timeout=0.01))
        with self.assertRaisesRegex(agv.AGVError, "确认超时"):
            backend.run()
        backend.cleanup()
        self.assertFalse(any(a == 4 and v for a, v, t in self.bus.writes))

    def test_reject_loaded_confirmation_prevents_travel_and_cleans_both(self):
        backend = self.backend(replace(self.settings, material_input=None, loaded_level=None))
        original = self.publish
        def publish(kind, **data):
            if kind == EventKind.CONFIRMATION and data["key"] == "loaded":
                self.auto_answer = False
                self.control.answer(data["request_id"], False)
            original(kind, **data)
        backend.publish = publish
        with patch.object(agv, "time", self.clock), patch.object(warehouse_plc, "time", self.clock):
            with self.assertRaises(TaskCancelled):
                backend.run()
            report = backend.cleanup()
        self.assertNotIn((1, 1), self.client.commands)
        self.assertTrue(report["agv"]["travel_stationary"])
        self.assertTrue(self.bus.closed)

    def test_cancel_during_loading_stops_and_does_not_travel(self):
        backend = self.backend()
        self.client.on_command = lambda f, d: self.control.cancel() if (f, d) == (23, 2) else None
        with patch.object(agv, "time", self.clock), patch.object(warehouse_plc, "time", self.clock):
            with self.assertRaises(TaskCancelled):
                backend.run()
            report = backend.cleanup()
        self.assertNotIn((1, 1), self.client.commands)
        self.assertTrue(report["plc_command_clear"])
        self.assertEqual(self.client.commands[-3:], [(2, 0), (23, 0), (6, 0)])

    def test_load_response_loss_does_not_retry_or_travel(self):
        self.client.fail_load = True
        backend = self.backend()
        with patch.object(agv, "time", self.clock), patch.object(warehouse_plc, "time", self.clock):
            with self.assertRaises(TimeoutError):
                backend.run()
            backend.cleanup()
        self.assertEqual(self.client.commands.count((23, 2)), 1)
        self.assertNotIn((1, 1), self.client.commands)

    def test_plc_connection_failure_still_shuts_down_connected_agv(self):
        self.bus.fail_connect = True
        backend = self.backend()
        with self.assertRaises(warehouse_plc.WarehouseError):
            backend.run()
        backend.cleanup()
        self.assertEqual(self.client.commands, [(2, 0), (23, 0), (6, 0)])
        self.assertTrue(self.client.closed and self.bus.closed)

    def test_agv_connection_failure_does_not_reconnect_or_send_actions(self):
        self.client.fail_connect = True
        backend = self.backend()
        with self.assertRaises(ConnectionError):
            backend.run()
        report = backend.cleanup()
        self.assertEqual(self.client.commands, [])
        self.assertTrue(report["agv"]["connection_closed"])

    def test_cleanup_failures_keep_independent_results_and_close_both(self):
        backend = self.backend()
        with patch.object(agv, "time", self.clock), patch.object(warehouse_plc, "time", self.clock):
            backend.run()
        self.plc.command_active = True
        self.bus.fail_clear = True
        self.client.fail_stop = True
        with self.assertRaises(CleanupError) as caught:
            backend.cleanup()
        report = caught.exception.report
        self.assertFalse(report["plc_command_clear"])
        self.assertFalse(report["agv"]["travel_stop_ack"])
        self.assertTrue(report["agv"]["roller_stop_ack"])
        self.assertTrue(report["plc_connection_closed"] and report["agv"]["connection_closed"])

    def test_initial_station_zero_is_raw_and_only_confirmed_after_preparation(self):
        self.client.station = 0
        self.run_fast(self.backend())
        devices = [d for k, d in self.events if k == EventKind.DEVICE]
        initial = next(d for d in devices if "raw_station" in d)
        self.assertEqual(initial["raw_station"], 0)
        self.assertIsNone(initial["confirmed_station"])
        self.assertIn({"confirmed_station": 1, "target_station": None, "in_transit": False}, devices)

    def test_worker_marks_real_events_and_cleanup_failure_cannot_succeed(self):
        self.client.fail_stop = True
        factory = partial(RealBackend, agv_factory=lambda *a: self.client, plc_factory=self.plc_factory)
        worker = TaskWorker(self.parameters, self.settings, self.control, factory, {}, mode="real")
        events = []
        def answer(event):
            events.append(event)
            if event.kind == EventKind.CONFIRMATION:
                self.control.answer(event.data["request_id"], True)
        worker.event.connect(answer)
        with patch.object(agv, "time", self.clock), patch.object(warehouse_plc, "time", self.clock):
            worker.run()
        self.assertEqual(events[-1].data["outcome"], "failed")
        self.assertFalse(events[-1].data["cleanup_ok"])
        self.assertTrue(events[-1].data["cleanup_report"]["agv"]["connection_closed"])
        self.assertTrue(all(e.mode == "real" for e in events))

    def test_poll_confirmation_timeout_preserves_request_and_cancel_wakes(self):
        self.control.prepare_confirmation("one")
        with self.assertRaises(queue.Empty):
            self.control.poll_confirmation("one", 0.001)
        self.assertEqual(self.control.pending_id, "one")
        self.control.answer("one", True)
        self.assertEqual(self.control.poll_confirmation("one", 0.001), "")
        self.control.prepare_confirmation("two")
        self.control.cancel()
        with self.assertRaises(TaskCancelled):
            self.control.poll_confirmation("two", 0.001)

    def test_cancel_check_prevents_real_agv_send_and_plc_write(self):
        client = agv.AGVClient()
        client.socket = Mock()
        plc = self.plc_factory()
        plc.cancel_check = self.control.checkpoint
        self.control.cancel()
        with agv.execution_hooks(agv.ExecutionHooks(cancel_check=self.control.checkpoint)):
            with self.assertRaises(TaskCancelled):
                client.command(1, 1, "后退")
        with self.assertRaises(TaskCancelled):
            plc.write(4, 1)
        client.socket.sendall.assert_not_called()
        self.assertEqual(self.bus.writes, [])

    def test_gui_hooks_do_not_leak_into_cli_or_other_threads(self):
        calls = []
        hooks = agv.ExecutionHooks(cancel_check=lambda: calls.append("worker"))
        with agv.execution_hooks(hooks):
            agv.checkpoint()
            thread = threading.Thread(target=agv.checkpoint)
            thread.start()
            thread.join(1)
        agv.checkpoint()
        self.assertEqual(calls, ["worker"])


if __name__ == "__main__":
    unittest.main()
