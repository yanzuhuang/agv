"""合并流程离线测试，不连接真实 PLC 或 AGV。"""

import contextlib
import io
import types
import unittest
from unittest.mock import Mock, patch

import warehouse_agv as task
from test_agv_task import FakeAGV, FakeClock


class CombinedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.args = task.parse_args([
            "--dry-run", "--column", "6", "--level", "1", "--duration", "5",
            "--roller-profile", "pdf", "--material-input", "2", "--loaded-level", "1",
            "--poll-interval", "0.001",
            "--inbound-column", "4", "--inbound-level", "3",
        ])
        self.client = FakeAGV()
        self.plc = Mock()
        self.warehouse = Mock()
        def skip_wait(deadline, feeder=None, feed_deadline=None):
            if feeder is not None:
                feeder.stop_feed(wait=False)
        for target in ("outbound_agv.wait_confirmation", "outbound_agv.wait_timed"):
            p = patch(target, side_effect=skip_wait) if target.endswith("wait_timed") else patch(target)
            p.start()
            self.addCleanup(p.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_line_overlaps_loading_and_unloading_then_correct_route(self):
        events = []
        original = self.client.command

        def command(function, data, label):
            events.append((function, data))
            return original(function, data, label)

        self.client.command = command
        self.warehouse.outbound.side_effect = lambda *args: events.append("outbound")
        self.warehouse.inbound.side_effect = lambda *args: events.append("inbound")
        self.plc.begin.side_effect = lambda command: events.append("line-out" if command == 2 else "line-in")
        task.deliver(self.client, self.plc, self.args, self.warehouse)
        self.warehouse.outbound.assert_called_once_with(self.plc, 6, 1)
        self.warehouse.inbound.assert_called_once_with(self.plc, 4, 3)
        self.assertEqual(events, [
            (2, 0), (0x17, 0), (6, 0), "outbound", "line-out",
            (0x17, 2), (0x17, 0), (6, 2), (1, 1), (2, 0),
            (6, 0), (6, 1), (1, 0), (2, 0), "line-in", (0x17, 3), (0x17, 0),
            "inbound",
        ])
        self.assertEqual(self.client.station, 1)
        self.assertFalse(self.client.material)

    def test_warehouse_failure_prevents_loading_and_travel(self):
        self.warehouse.outbound.side_effect = TimeoutError("线体停止未确认")
        with self.assertRaises(TimeoutError):
            task.deliver(self.client, self.plc, self.args, self.warehouse)
        self.assertEqual(self.client.commands, [(2, 0), (0x17, 0), (6, 0)])

    def test_load_failure_prevents_travel(self):
        self.client.fail_load = True
        with self.assertRaises(TimeoutError):
            task.deliver(self.client, self.plc, self.args, self.warehouse)
        self.assertNotIn((1, 1), self.client.commands)

    def test_wrong_start_station_prevents_warehouse_action(self):
        self.client.station = 2
        with self.assertRaises(task.agv.AGVError):
            task.deliver(self.client, self.plc, self.args, self.warehouse)
        self.warehouse.outbound.assert_not_called()

    def test_initial_zero_allows_loading_and_keeps_target_commands(self):
        self.client.station = 0
        task.deliver(self.client, self.plc, self.args, self.warehouse)
        self.warehouse.outbound.assert_called_once()
        self.warehouse.inbound.assert_called_once()
        self.assertEqual([data for function, data in self.client.commands if function == 6],
                         [0, 2, 0, 1])
        self.assertEqual(self.client.station, 1)
        self.assertFalse(self.client.material)
        self.assertTrue(task.agv.wait_confirmation.call_args.kwargs["allow_initial_zero"])

    def test_initial_zero_still_rejects_motion_and_alarm(self):
        self.client.station = 0
        initial = self.client.status()
        for changes in ({"speed_percent": 20}, {"alarm_io": 4}, {"state_code": 0}):
            with self.subTest(changes=changes), patch.object(
                    self.client, "status", return_value={**initial, **changes}):
                with self.assertRaises(task.agv.AGVError):
                    task.deliver(self.client, self.plc, self.args, self.warehouse)
                self.warehouse.outbound.assert_not_called()
                self.assertEqual(self.client.commands, [])

    def test_return_zero_is_not_accepted_as_station_one_arrival(self):
        self.client.station = 0
        self.args.travel_timeout = 0.003
        original_status = self.client.status

        def raw_status(timeout=None):
            value = original_status(timeout)
            if (1, 0) in self.client.commands:
                value["station"] = 0
            return value

        with patch.object(self.client, "status", side_effect=raw_status):
            with self.assertRaises(task.agv.AGVError):
                task.deliver(self.client, self.plc, self.args, self.warehouse)
        self.assertNotIn((0x17, 3), self.client.commands)
        self.warehouse.inbound.assert_not_called()

    def test_outbound_entrypoint_also_accepts_initial_zero(self):
        self.client.station = 0
        args = task.agv.parse_args(["--agv-only", "--roller-profile", "pdf",
                                   "--poll-interval", "0.001"])
        task.agv.deliver(self.client, args)
        self.assertEqual(self.client.station, 1)
        self.assertIn((0x17, 2), self.client.commands)
        self.assertIn((0x17, 3), self.client.commands)

    def test_interactive_parameters(self):
        with patch("builtins.input", side_effect=["", "", "", "6", "1", "5", "word", "4", "3"]):
            args = task.parse_args([])
        self.assertEqual((args.plc_host, args.plc_port, args.device_id), ("192.168.1.11", 502, 1))
        self.assertEqual((args.column, args.level, args.duration, args.roller_profile), (6, 1, 5, "word"))
        self.assertEqual((args.inbound_column, args.inbound_level), (4, 3))

    def test_invalid_duration_rejected_before_prompt(self):
        for value in ("0", "-1", "1", "2", "nan", "inf", "181"):
            with self.subTest(value=value), patch("builtins.input") as prompt:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    task.parse_args(["--duration", value])
                prompt.assert_not_called()

    def test_dry_run_and_exit_never_connect(self):
        with patch.object(task.agv, "AGVClient") as client:
            self.assertEqual(task.main(["--dry-run", "--column", "1", "--level", "1",
                                        "--duration", "5", "--roller-profile", "word",
                                        "--inbound-column", "1", "--inbound-level", "1"]), 0)
            self.assertEqual(task.main(["--operation", "exit"]), 0)
            client.assert_not_called()

    def test_failure_cleans_up_both_devices(self):
        self.args.dry_run = False
        warehouse = types.ModuleType("warehouse_modbus")
        warehouse.WarehousePLC = Mock(return_value=self.plc)
        warehouse.outbound = Mock(side_effect=TimeoutError("PLC timeout"))
        with patch.object(task, "parse_args", return_value=self.args), \
                patch.object(task.agv, "AGVClient") as client_class, \
                patch.dict("sys.modules", {"warehouse_modbus": warehouse}):
            client = client_class.return_value
            client.status.return_value = self.client.status()
            self.assertEqual(task.main([]), 1)
        self.plc.close.assert_called_once()
        client.shutdown.assert_called_once()
        self.assertFalse(any(call.args[:2] == (1, 1) for call in client.command.call_args_list))

    def test_real_warehouse_driver_with_simulated_modbus(self):
        import warehouse_modbus
        from warehouse_plc import WarehousePLC

        # 出库：空闲 -> 忙碌 -> 空闲；线体：空闲 -> 忙碌 -> 空闲。
        # 出库、出货线体、入货线体、回库，每段均握手并确认停止。
        states = iter([0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0])
        bus = Mock()
        bus.read_holding_registers.side_effect = lambda address, **kwargs: types.SimpleNamespace(
            isError=lambda: False, registers=[next(states) if address == 0 else 1])
        bus.write_register.return_value = types.SimpleNamespace(isError=lambda: False)
        plc = WarehousePLC(client=bus)
        events = []
        original = self.client.command

        def command(function, data, label):
            if (function, data) == (0x17, 2):
                # 上料时线体命令2仍然活动，尚未清零。
                self.assertEqual([call.args for call in bus.write_register.call_args_list],
                                 [(4, 0), (1, 6), (2, 1), (3, 1), (4, 1),
                                  (4, 0), (4, 0), (4, 2)])
                self.assertTrue(plc.command_active)
                events.append("load-while-line-active")
            return original(function, data, label)

        self.client.command = command
        task.deliver(self.client, plc, self.args, warehouse_modbus)
        self.assertEqual(events, ["load-while-line-active"])
        self.assertEqual([call.args for call in bus.write_register.call_args_list][9:],
                         [(4, 0), (4, 3), (4, 0),
                          (4, 0), (1, 4), (2, 3), (3, 1), (4, 4), (4, 0)])
        bus.connect.assert_not_called()

    def test_unloading_failure_prevents_inbound(self):
        original = self.client.command

        def command(function, data, label):
            if (function, data) == (0x17, 3):
                raise TimeoutError("下料应答丢失")
            return original(function, data, label)

        self.client.command = command
        with self.assertRaises(TimeoutError):
            task.deliver(self.client, self.plc, self.args, self.warehouse)
        self.warehouse.inbound.assert_not_called()

    def test_line_inbound_failure_prevents_storage(self):
        import warehouse_modbus

        self.plc.wait_status.side_effect = [None, TimeoutError("线体入货停止未确认")]
        with patch.object(warehouse_modbus, "outbound"):
            with self.assertRaises(TimeoutError):
                task.deliver(self.client, self.plc, self.args, warehouse_modbus)
        self.plc.move.assert_not_called()

    def test_invalid_inbound_slot_rejected_before_prompt(self):
        for option, value in (("--inbound-column", "0"), ("--inbound-column", "7"),
                              ("--inbound-level", "0"), ("--inbound-level", "4")):
            with self.subTest(option=option, value=value), patch("builtins.input") as prompt:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    task.parse_args([option, value])
                prompt.assert_not_called()


class InitialPositionChecks(unittest.TestCase):
    def test_preparation_confirmation_accepts_zero_without_changing_raw_status(self):
        import queue

        initial = {"station": 0, "speed_percent": 0, "state_code": 0x0E,
                   "state": "parked", "alarm_io": 0}
        client = Mock()
        client.status.return_value = initial
        args = types.SimpleNamespace(transfer_timeout=1, poll_interval=0.001)
        answers = queue.Queue()
        answers.put("")
        with patch.object(task.agv, "confirmation_reader", return_value=answers):
            task.agv.wait_confirmation(client, 1, args, "ready", allow_initial_zero=True)
        self.assertEqual(initial["station"], 0)
        with self.assertRaises(task.agv.AGVError):
            task.agv.require_stationary(initial, 1)
        with self.assertRaises(task.agv.AGVError):
            task.agv.require_stationary(initial, 2, allow_initial_zero=True)


class CombinedTimingTests(unittest.TestCase):
    def test_complete_route_uses_same_duration_and_only_one_infeed(self):
        for duration in (5, 8):
            with self.subTest(duration=duration):
                args = task.parse_args([
                    "--dry-run", "--column", "6", "--level", "1",
                    "--duration", str(duration), "--roller-profile", "pdf",
                    "--material-input", "2", "--loaded-level", "1",
                    "--inbound-column", "4", "--inbound-level", "3",
                ])
                client, plc, warehouse, clock = FakeAGV(), Mock(), Mock(), FakeClock()
                events = []
                original = client.command

                def command(function, data, label):
                    events.append((clock.now, "agv", function, data))
                    return original(function, data, label)

                client.command = command
                plc.begin.side_effect = lambda code: events.append((clock.now, "line", code))
                plc.clear_command.side_effect = lambda: events.append((clock.now, "line", 0))
                warehouse.inbound.side_effect = lambda *a: events.append((clock.now, "store"))
                with patch("outbound_agv.time.monotonic", side_effect=clock.monotonic), \
                        patch("outbound_agv.time.sleep", side_effect=clock.sleep), \
                        patch("outbound_agv.wait_confirmation"), contextlib.redirect_stdout(io.StringIO()):
                    task.deliver(client, plc, args, warehouse)
                line = [event for event in events if event[1] == "line"]
                self.assertEqual([event[2] for event in line], [2, 0, 3, 0])
                for start, stop in (line[:2], line[2:]):
                    self.assertAlmostEqual(stop[0] - start[0], duration)
                load = next(e for e in events if e[1:] == ("agv", 0x17, 2))
                unload = next(e for e in events if e[1:] == ("agv", 0x17, 3))
                self.assertAlmostEqual(load[0] - line[0][0], duration - 2)
                self.assertAlmostEqual(unload[0], line[2][0])
                for start in (load, unload):
                    stop = next(e for e in events[events.index(start) + 1:] if e[1:] == ("agv", 0x17, 0))
                    self.assertAlmostEqual(stop[0] - start[0], 2)
                self.assertGreater(events.index(next(e for e in events if e[1] == "store")),
                                   events.index(line[-1]))
                plc.line_inbound.assert_not_called()
                warehouse.inbound.assert_called_once_with(plc, 4, 3)


if __name__ == "__main__":
    unittest.main()
