"""AGV 单体到站隔离测试；不连接真实设备、不创建 PLC 客户端。"""

from dataclasses import replace
from functools import partial
import unittest
from unittest.mock import Mock, patch

import outbound_agv as agv
from gui.agv_move import RealAgvMoveBackend, SimulationAgvMoveBackend
from gui.models import AgvMoveParameters, EventKind
from gui.settings import DeviceSettings
from gui.task_worker import TaskCancelled, TaskControl, TaskWorker
from test_real_backend import ConnectedFakeAGV
from test_agv_task import FakeClock


class AgvMoveTests(unittest.TestCase):
    def setUp(self):
        self.settings = DeviceSettings(poll_interval=0.001)
        self.parameters = AgvMoveParameters()
        self.client = ConnectedFakeAGV()
        self.client.station = 2
        self.control = TaskControl()
        self.clock = FakeClock()
        self.events = []
        self.auto_confirm = True
        self.plc_factory = Mock(side_effect=AssertionError("AGV 单体不能创建 PLC"))
        self.addCleanup(self.plc_factory.assert_not_called)
        for target in ("socket.create_connection", "socket.socket", "builtins.input",
                       "pymodbus.client.ModbusTcpClient.connect"):
            guard = patch(target, side_effect=AssertionError(target))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)

    def publish(self, kind, **data):
        self.events.append((kind, data))
        if kind == EventKind.CONFIRMATION and self.auto_confirm:
            self.control.answer(data["request_id"], True)

    def backend(self, parameters=None, settings=None):
        return RealAgvMoveBackend(parameters or self.parameters, settings or self.settings,
                                  self.control, self.publish, agv_factory=lambda *a: self.client,
                                  plc_factory=self.plc_factory)

    def complete(self, backend):
        with patch.object(agv, "time", self.clock):
            backend.run()
            return backend.cleanup()

    def test_default_station_one_and_no_roller_profile_required(self):
        self.assertEqual(self.parameters.target_station, 1)
        self.parameters.validate(self.settings)
        report = self.complete(self.backend())
        self.assertEqual(self.client.station, 1)
        self.assertIsNone(report["plc_command_clear"])
        self.assertFalse(report["plc_was_connected"])

    def test_validation_rejects_clear_station_and_invalid_direction(self):
        for station in (0, -1, 256, True, 2.5):
            with self.subTest(station=station), self.assertRaises(ValueError):
                AgvMoveParameters(station).validate(self.settings)
        for station in (1, 10, 255):
            AgvMoveParameters(station).validate(self.settings)
        with self.assertRaises(ValueError):
            AgvMoveParameters(1, "automatic").validate(self.settings)

    def test_forward_to_station_seven_has_one_move_and_no_loading_actions(self):
        report = self.complete(self.backend(AgvMoveParameters(7, "forward")))
        self.assertEqual(self.client.commands, [(2, 0), (23, 0), (6, 0), (6, 7), (1, 0),
                                              (2, 0), (2, 0), (23, 0), (6, 0)])
        self.assertTrue(report["agv"]["travel_stationary"])
        self.assertTrue(report["agv"]["connection_closed"])
        self.assertFalse(any(f == 23 and d for f, d in self.client.commands))
        self.assertEqual([d["stage"] for k, d in self.events if k == EventKind.STAGE
                          and d["status"] == "completed"], [0, 1, 2])

    def test_backward_to_station_255_uses_explicit_direction(self):
        self.complete(self.backend(AgvMoveParameters(255, "backward")))
        self.assertIn((6, 255), self.client.commands)
        self.assertIn((1, 1), self.client.commands)
        self.assertNotIn((1, 0), self.client.commands)

    def test_already_parked_at_target_does_not_restart_motion(self):
        self.client.station = 1
        self.complete(self.backend())
        self.assertFalse(any(f == 1 or (f == 6 and d) for f, d in self.client.commands))
        self.assertTrue(any(k == EventKind.DEVICE and d.get("confirmed_station") == 1 for k, d in self.events))

    def test_initial_zero_is_not_assumed_to_be_station_one(self):
        self.client.station = 0
        self.complete(self.backend())
        first = next(d for k, d in self.events if k == EventKind.DEVICE and "raw_station" in d)
        self.assertEqual(first["raw_station"], 0)
        self.assertIsNone(first["confirmed_station"])
        self.assertIn((6, 1), self.client.commands)
        self.assertIn((1, 0), self.client.commands)

    def test_arrival_requires_two_consecutive_parking_samples(self):
        sequence = [(0x0E, 0), (0x0A, 20), (0x0E, 0), (0x0E, 0)]
        samples = []
        def status(value):
            if (1, 0) in self.client.commands and sequence:
                code, speed = sequence.pop(0)
                value.update(station=7, state_code=code, speed_percent=speed)
                samples.append((code, speed))
        self.client.on_status = status
        self.complete(self.backend(AgvMoveParameters(7)))
        self.assertEqual(samples, [(0x0E, 0), (0x0A, 20), (0x0E, 0), (0x0E, 0)])
        self.assertFalse(sequence)

    def test_initial_alarm_prevents_target_and_motion(self):
        self.client.on_status = lambda value: value.update(alarm_io=4)
        backend = self.backend()
        with self.assertRaises(agv.AGVError):
            backend.run()
        self.assertEqual(self.client.commands, [])
        self.client.on_status = None
        backend.cleanup()

    def test_reject_preparation_prevents_target_and_motion_and_cleans(self):
        backend = self.backend()
        def publish(kind, **data):
            self.events.append((kind, data))
            if kind == EventKind.CONFIRMATION:
                self.control.answer(data["request_id"], False)
        backend.publish = publish
        with self.assertRaises(TaskCancelled):
            backend.run()
        report = backend.cleanup()
        self.assertFalse(any(f == 1 or (f == 6 and d) for f, d in self.client.commands))
        self.assertTrue(report["agv"]["connection_closed"])

    def test_cancel_after_travel_starts_stops_without_confirming_arrival(self):
        self.client.on_command = lambda f, d: self.control.cancel() if f == 1 else None
        backend = self.backend(AgvMoveParameters(7))
        with self.assertRaises(TaskCancelled):
            backend.run()
        backend.cleanup()
        self.assertIsNone(backend.confirmed_station)
        self.assertEqual(self.client.commands[-3:], [(2, 0), (23, 0), (6, 0)])

    def test_travel_timeout_does_not_confirm_target(self):
        def status(value):
            if (1, 0) in self.client.commands:
                value.update(station=2, state_code=0x0A, speed_percent=20)
        self.client.on_status = status
        backend = self.backend(AgvMoveParameters(7), replace(self.settings, travel_timeout=0.005))
        with patch.object(agv, "time", self.clock):
            with self.assertRaisesRegex(agv.AGVError, "站点7超时"):
                backend.run()
        self.client.on_status = None
        backend.cleanup()
        self.assertFalse(any(k == EventKind.DEVICE and d.get("confirmed_station") == 7 for k, d in self.events))

    def test_lost_target_response_does_not_retry_or_start_motion(self):
        command = self.client.command
        def lost_response(function, data, label):
            result = command(function, data, label)
            if (function, data) == (6, 7):
                self.client.broken = True
                raise TimeoutError("目标指令应答丢失")
            return result
        self.client.command = lost_response
        backend = self.backend(AgvMoveParameters(7))
        with self.assertRaises(TimeoutError):
            backend.run()
        backend.cleanup()
        self.assertEqual(self.client.commands.count((6, 7)), 1)
        self.assertFalse(any(f == 1 for f, d in self.client.commands))

    def test_confirmation_wait_keeps_checking_alarm(self):
        self.auto_confirm = False
        def alarm(value):
            if self.client.reads >= 3:
                value["alarm_io"] = 4
        self.client.on_status = alarm
        backend = self.backend()
        with self.assertRaises(agv.AGVError):
            backend.run()
        self.client.on_status = None
        backend.cleanup()
        self.assertFalse(any(f == 1 for f, d in self.client.commands))

    def test_worker_uses_four_stages_and_marks_agv_task_events(self):
        factory = partial(RealAgvMoveBackend, agv_factory=lambda *a: self.client, plc_factory=self.plc_factory)
        worker = TaskWorker(self.parameters, self.settings, self.control, factory, {}, mode="real")
        events = []
        def answer(event):
            events.append(event)
            if event.kind == EventKind.CONFIRMATION:
                self.control.answer(event.data["request_id"], True)
        worker.event.connect(answer)
        with patch.object(agv, "time", self.clock):
            worker.run()
        self.assertEqual(events[-1].data["outcome"], "success")
        self.assertTrue(all(e.task_type == "agv_move" and e.mode == "real" for e in events))
        completed = [e.data["stage"] for e in events if e.kind == EventKind.STAGE and e.data["status"] == "completed"]
        self.assertEqual(completed, [0, 1, 2, 3])

    def test_simulation_works_without_roller_profile_and_has_only_agv_stages(self):
        backend = SimulationAgvMoveBackend(AgvMoveParameters(7), self.settings, self.control,
                                           self.publish, time_scale=0.001)
        backend.run()
        backend.cleanup()
        self.assertEqual([d["stage"] for k, d in self.events if k == EventKind.STAGE and d["status"] == "completed"], [0, 1, 2])
        self.assertTrue(any(k == EventKind.DEVICE and d.get("confirmed_station") == 7 for k, d in self.events))


if __name__ == "__main__":
    unittest.main()
