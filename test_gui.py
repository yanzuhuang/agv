"""Qt offscreen 验证：真实网络与 input() 均禁止，覆盖完整模拟任务。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QRawFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionSpinBox

from gui.main import create_application
from gui.main_window import MainWindow
from gui.models import EventKind, TaskParameters
from gui.settings import DeviceSettings, SettingsDialog, SettingsStore
from gui.task_controller import TaskController
from gui.task_worker import TaskCancelled, TaskControl, TaskWorker
from gui.simulation import SimulationBackend


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Path("tmp").mkdir(exist_ok=True)
        cls.app = create_application()

    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).parent / "tmp")
        self.store = SettingsStore(Path(self.temp.name) / "settings.ini")
        self.settings = DeviceSettings(roller_profile="word")
        self.parameters = TaskParameters(6, 1, 6, 1, 5)
        self.windows = []
        self.controllers = []
        self.network_guards = []
        for name in ("socket.create_connection", "socket.socket",
                     "pymodbus.client.ModbusTcpClient.connect", "builtins.input"):
            guard = patch(name, side_effect=AssertionError(f"模拟 GUI 禁止调用 {name}"))
            self.network_guards.append(guard.start())
            self.addCleanup(guard.stop)

    def tearDown(self):
        for window in self.windows:
            window.controller.cancel()
        for controller in self.controllers:
            controller.cancel()
        for controller in self.controllers + [w.controller for w in self.windows]:
            self.wait_until(lambda c=controller: not c.busy)
        for window in self.windows:
            window.close()
        self.app.processEvents()
        self.temp.cleanup()
        for guard in self.network_guards:
            guard.assert_not_called()

    def wait_until(self, predicate, timeout=4000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Qt 事件或后台任务未在规定时间内完成。")
            QTest.qWait(5)
        self.app.processEvents()

    def window(self, configured=True, time_scale=0.008):
        if configured:
            self.store.save(self.settings)
        window = MainWindow(self.store, time_scale=time_scale)
        self.windows.append(window)
        window.show()
        QTest.qWait(10)
        return window

    def controller(self):
        controller = TaskController()
        self.controllers.append(controller)
        return controller

    def auto_confirm(self, controller):
        def answer(event):
            if event.kind == EventKind.CONFIRMATION:
                controller.answer_confirmation(event.data["request_id"], True)
        controller.event.connect(answer)

    def run_controller(self, scenario="normal", settings=None):
        controller = self.controller()
        events = []
        controller.event.connect(events.append)
        self.auto_confirm(controller)
        controller.start(self.parameters, settings or self.settings, time_scale=0.003, scenario=scenario)
        self.wait_until(lambda: not controller.busy)
        return controller, events

    def test_initial_window_is_simulation_and_real_mode_requires_explicit_choice(self):
        window = self.window(configured=False)
        self.assertIn("模拟模式", window.windowTitle())
        self.assertIn("模拟模式", window.plc_badge.text())
        self.assertIn("模拟模式", window.agv_badge.text())
        self.assertNotIn("已连接", window.plc_badge.text())
        self.assertTrue(window.mode_combo.model().item(1).isEnabled())
        self.assertEqual(window.mode_combo.currentData(), "simulation")
        self.assertEqual(window.duration_input.value(), 5)
        self.assertTrue(window.start_button.isEnabled())
        self.assertTrue(window.stop_button.isEnabled())
        self.assertEqual(window.stage_states, ["waiting"] * 8)

    def test_chinese_font_is_available_for_actual_rendering(self):
        self.assertTrue(QRawFont.fromFont(self.app.font()).supportsCharacter(ord("仓")))

    def test_storage_grid_orientation_and_all_18_slots(self):
        window = self.window()
        grid = window.storage_grid
        self.assertEqual(len(grid.buttons), 18)
        self.assertLess(grid.buttons[(1, 3)].y(), grid.buttons[(1, 1)].y())
        self.assertLess(grid.buttons[(1, 1)].x(), grid.buttons[(6, 1)].x())
        for column, level in grid.buttons:
            QTest.mouseClick(grid.buttons[(column, level)], Qt.MouseButton.LeftButton)
            self.assertEqual(window.outbound_column.value(), column)
            self.assertEqual(window.outbound_level.value(), level)

    def test_grid_and_inputs_sync_in_both_directions(self):
        window = self.window()
        window.outbound_column.setValue(2)
        window.outbound_level.setValue(3)
        self.assertEqual(window.storage_grid.outbound, (2, 3))
        QTest.mouseClick(window.inbound_radio, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.storage_grid.buttons[(4, 2)], Qt.MouseButton.LeftButton)
        self.assertEqual((window.inbound_column.value(), window.inbound_level.value()), (4, 2))
        self.assertEqual(window.storage_grid.buttons[(2, 3)].property("selection"), "outbound")
        self.assertEqual(window.storage_grid.buttons[(4, 2)].property("selection"), "inbound")

    def test_same_slot_expresses_both_choices_without_stock_claims(self):
        window = self.window()
        button = window.storage_grid.buttons[(6, 1)]
        self.assertEqual(button.property("selection"), "both")
        self.assertIn("出 / 入", button.text())
        for button in window.storage_grid.buttons.values():
            self.assertNotIn("空库位", button.text())
            self.assertNotIn("有货", button.text())

    def test_explicit_roller_profile_required_before_start(self):
        window = self.window(configured=False)
        window.start_task()
        self.assertFalse(window.controller.busy)
        self.assertIn("显式选择", window.validation_label.text())
        self.assertIsNone(window.confirmation_dialog)

    def test_duration_must_exceed_two_and_not_action_timeout(self):
        window = self.window()
        for value in (2, 1, 181):
            window.duration_input.setValue(value)
            window.start_task()
            self.assertFalse(window.controller.busy)
            self.assertIn("大于 2", window.validation_label.text())
        TaskParameters(1, 1, 1, 1, 2.01).validate(self.settings)

    def test_invalid_settings_and_task_ranges_are_rejected(self):
        bad_settings = (
            replace(self.settings, plc_host=" "), replace(self.settings, port=0),
            replace(self.settings, device_id=248), replace(self.settings, agv_id=256),
            replace(self.settings, timeout=float("nan")), replace(self.settings, poll_interval=0),
            replace(self.settings, roller_profile="unknown"), replace(self.settings, query_data=2),
        )
        for config in bad_settings:
            with self.subTest(config=config), self.assertRaises(ValueError):
                config.validate()
        for parameters in (TaskParameters(0, 1, 1, 1, 5), TaskParameters(1, 4, 1, 1, 5),
                           TaskParameters(1, 1, 7, 1, 5), TaskParameters(1, 1, 1, 1, float("inf"))):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                parameters.validate(self.settings)

    def test_material_io_and_level_must_be_paired(self):
        for config in (replace(self.settings, material_input=2), replace(self.settings, loaded_level=1),
                       replace(self.settings, material_input=9, loaded_level=1),
                       replace(self.settings, material_input=2, loaded_level=2)):
            with self.assertRaises(ValueError):
                config.validate()
        replace(self.settings, material_input=2, loaded_level=0).validate()

    def test_settings_persist_and_reload(self):
        config = replace(self.settings, plc_host="192.168.1.20", port=1234,
                         roller_profile="pdf", material_input=3, loaded_level=0, poll_interval=0.25,
                         timeout=2.75, action_timeout=180.5, travel_timeout=90.25)
        self.store.save(config)
        fresh = SettingsStore(self.store.filename)
        self.assertEqual(fresh.load(), config)

    def test_corrupt_settings_restore_defaults_without_auto_profile(self):
        self.store.qsettings.setValue("action_timeout", "broken")
        self.store.qsettings.sync()
        store = SettingsStore(self.store.filename)
        self.assertEqual(store.load(), DeviceSettings())
        self.assertTrue(store.load_error)
        self.assertIsNone(store.load().roller_profile)

    def test_settings_dialog_enforces_pair_and_saves_explicit_choice(self):
        dialog = SettingsDialog(DeviceSettings(), self.store)
        self.assertIsNone(dialog.inputs["roller_profile"].currentData())
        self.assertFalse(dialog.inputs["loaded_level"].isEnabled())
        dialog.inputs["roller_profile"].setCurrentIndex(1)
        dialog.inputs["material_input"].setCurrentIndex(2)
        dialog._save()
        self.assertIn("成对", dialog.error_label.text())
        dialog.inputs["loaded_level"].setCurrentIndex(2)
        dialog._save()
        self.assertEqual(self.store.load().material_input, 2)
        self.assertEqual(self.store.load().loaded_level, 1)
        dialog.deleteLater()

    def test_ui_manual_confirmations_gate_actions_and_complete(self):
        window = self.window()
        confirmations = []
        window.controller.event.connect(lambda event: confirmations.append(event.data["key"])
                                        if event.kind == EventKind.CONFIRMATION else None)
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        QTest.qWait(25)
        self.assertEqual(window.stage_states[1], "waiting")
        QTest.mouseClick(window.confirm_button, Qt.MouseButton.LeftButton)
        self.wait_until(lambda: window.confirmation_dialog is not None)
        self.assertEqual(window.stage_states[3], "waiting")
        QTest.mouseClick(window.confirm_button, Qt.MouseButton.LeftButton)
        self.wait_until(lambda: not window.controller.busy)
        self.assertEqual(confirmations, ["prepare", "loaded"])
        self.assertEqual(window.stage_states, ["completed"] * 8)
        self.assertIn("模拟完成", window.task_badge.text())
        self.assertTrue(window.start_button.isEnabled())
        self.assertTrue(window.parameter_panel.isEnabled())
        self.assertTrue(window.stop_button.isEnabled())
        self.assertIn("站点 1", window.agv_labels["confirmed_station"].text())
        self.assertIn("24.50 V", window.agv_labels["voltage"].text())
        self.assertNotIn("%", window.agv_labels["voltage"].text())

    def test_io_mode_still_requests_preparation_but_skips_loaded_dialog(self):
        config = replace(self.settings, material_input=2, loaded_level=1)
        controller, events = self.run_controller(settings=config)
        confirmations = [e.data["key"] for e in events if e.kind == EventKind.CONFIRMATION]
        self.assertEqual(confirmations, ["prepare"])
        self.assertEqual(controller.last_result.data["outcome"], "success")

    def test_worker_events_follow_full_order_and_result_follows_cleanup(self):
        controller, events = self.run_controller()
        started = [e.data["stage"] for e in events if e.kind == EventKind.STAGE
                   and e.data["status"] == "running"]
        self.assertEqual(list(dict.fromkeys(started)), list(range(8)))
        self.assertLess(next(i for i, e in enumerate(events) if e.kind == EventKind.CLEANUP),
                        next(i for i, e in enumerate(events) if e.kind == EventKind.RESULT))
        self.assertTrue(controller.last_result.data["cleanup_ok"])
        confirmed = [e.data["confirmed_station"] for e in events if e.kind == EventKind.DEVICE
                     and e.data.get("confirmed_station") is not None]
        self.assertEqual(confirmed, [1, 2, 1])

    def test_repeat_start_prohibited_and_inputs_locked(self):
        window = self.window()
        window.start_task()
        self.assertFalse(window.parameter_panel.isEnabled())
        self.assertFalse(window.start_button.isEnabled())
        self.assertFalse(window.settings_button.isEnabled())
        self.assertFalse(window.top_settings_button.isEnabled())
        self.assertFalse(window.controller.start(self.parameters, self.settings))
        window.request_stop()
        self.wait_until(lambda: not window.controller.busy)
        self.assertTrue(window.start_button.isEnabled())

    def test_reject_preparation_cancels_and_does_not_outbound(self):
        window = self.window()
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        window.confirmation_dialog.reject()
        self.wait_until(lambda: not window.controller.busy)
        self.assertEqual(window.stage_states[0], "cancelled")
        self.assertEqual(window.stage_states[1:7], ["waiting"] * 6)
        self.assertEqual(window.controller.last_result.data["outcome"], "cancelled")
        self.assertTrue(window.start_button.isEnabled())

    def test_stop_while_waiting_loaded_confirmation_prevents_travel(self):
        window = self.window()
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        window.confirmation_dialog.accept()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        window.request_stop()
        self.wait_until(lambda: not window.controller.busy)
        self.assertEqual(window.stage_states[2], "cancelled")
        self.assertEqual(window.stage_states[3:7], ["waiting"] * 4)
        self.assertIsNone(window.confirmation_dialog)

    def test_stop_during_travel_does_not_start_return_or_unload(self):
        controller = self.controller()
        events = []
        self.auto_confirm(controller)
        controller.event.connect(events.append)
        controller.start(self.parameters, self.settings, time_scale=0.02)
        self.wait_until(lambda: any(e.kind == EventKind.DEVICE and e.data.get("in_transit") for e in events))
        controller.cancel()
        self.wait_until(lambda: not controller.busy)
        starts = [e.data["stage"] for e in events if e.kind == EventKind.STAGE and e.data["status"] == "running"]
        self.assertNotIn(4, starts)
        self.assertNotIn(5, starts)
        self.assertNotIn(6, starts)
        self.assertEqual(controller.last_result.data["outcome"], "cancelled")
        self.assertTrue(controller.last_result.data["cleanup_ok"])

    def test_loading_feedback_failure_does_not_drive(self):
        controller, events = self.run_controller("loading_failure")
        self.assertEqual(controller.last_result.data["outcome"], "failed")
        self.assertTrue(any(e.kind == EventKind.STAGE and e.data["stage"] == 2
                            and e.data["status"] == "failed" for e in events))
        self.assertFalse(any(e.kind == EventKind.STAGE and e.data["stage"] in (3, 4, 5, 6) for e in events))

    def test_arrival_timeout_does_not_confirm_station_two(self):
        controller, events = self.run_controller("travel_failure")
        self.assertEqual(controller.last_result.data["outcome"], "failed")
        self.assertFalse(any(e.kind == EventKind.DEVICE and e.data.get("confirmed_station") == 2 for e in events))

    def test_cleanup_failure_prevents_success(self):
        controller, events = self.run_controller("cleanup_failure")
        self.assertEqual(controller.last_result.data["outcome"], "failed")
        self.assertFalse(controller.last_result.data["cleanup_ok"])
        self.assertTrue(any(e.kind == EventKind.CLEANUP and not e.data["ok"] for e in events))

    def test_failed_window_restores_controls_and_marks_failed_stage(self):
        window = self.window()
        window.scenario_combo.setCurrentIndex(1)
        self.auto_confirm(window.controller)
        window.start_task()
        self.wait_until(lambda: not window.controller.busy)
        self.assertEqual(window.stage_states[2], "failed")
        self.assertEqual(window.stage_rows[2][0].property("stageState"), "failed")
        self.assertIn("模拟失败", window.task_badge.text())
        self.assertTrue(window.start_button.isEnabled())
        self.assertTrue(window.settings_button.isEnabled())

    def test_restart_after_cancel_resets_stages_and_confirmations(self):
        window = self.window()
        requests = []
        window.controller.event.connect(lambda event: requests.append(event.data["request_id"])
                                        if event.kind == EventKind.CONFIRMATION else None)
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        stale_id = requests[0]
        window.request_stop()
        self.wait_until(lambda: not window.controller.busy)
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        self.assertNotEqual(stale_id, requests[1])
        self.assertFalse(window.controller.answer_confirmation(stale_id, True))
        self.assertEqual(window.stage_states[1], "waiting")
        self.auto_confirm(window.controller)
        window.confirmation_dialog.accept()
        self.wait_until(lambda: not window.controller.busy)
        self.assertEqual(window.stage_states, ["completed"] * 8)

    def test_confirmation_timeout_fails_and_dismisses_dialog(self):
        window = self.window()
        window.settings = replace(self.settings, transfer_timeout=0.05)
        window.start_task()
        self.wait_until(lambda: not window.controller.busy)
        self.assertEqual(window.controller.last_result.data["outcome"], "failed")
        self.assertIn("确认超时", window.controller.last_result.data["message"])
        self.assertIsNone(window.confirmation_dialog)

    def test_main_thread_heartbeat_continues_while_worker_runs(self):
        controller = self.controller()
        self.auto_confirm(controller)
        beats = []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start(2)
        controller.start(self.parameters, self.settings, time_scale=0.015)
        self.wait_until(lambda: not controller.busy)
        timer.stop()
        self.assertGreater(len(beats), 10)

    def test_close_window_while_waiting_confirmation_joins_worker(self):
        window = self.window()
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        window.close()
        self.wait_until(lambda: not window.controller.busy and not window.isVisible())
        self.assertIsNone(window.controller._thread)
        self.assertIsNone(window.confirmation_dialog)

    def test_close_window_during_active_step_does_not_destroy_running_thread(self):
        window = self.window(time_scale=0.02)
        self.auto_confirm(window.controller)
        window.start_task()
        self.wait_until(lambda: window.stage_states[3] == "running")
        window.close()
        self.wait_until(lambda: not window.controller.busy and not window.isVisible())
        self.assertEqual(window.controller.last_result.data["outcome"], "cancelled")

    def test_real_mode_cannot_be_started_without_confirmation(self):
        controller = self.controller()
        with self.assertRaisesRegex(ValueError, "明确确认"):
            controller.start(self.parameters, self.settings, mode="real")
        self.assertFalse(controller.busy)
        self.assertIsNone(controller._thread)

    def test_logs_escape_markup_and_clear_only_display(self):
        window = self.window()
        window._append_log("ERROR", "<b>模拟错误</b>")
        self.assertIn("[ERROR] <b>模拟错误</b>", window.log_view.toPlainText())
        window.clear_logs_button.click()
        self.assertEqual(window.log_view.toPlainText(), "")

    def test_layout_at_requested_resolutions_and_export_previews(self):
        window = self.window()
        for width, height in ((1366, 768), (1920, 1080)):
            window.resize(width, height)
            QTest.qWait(20)
            self.assertLessEqual(window.centralWidget().width(), width)
            self.assertLessEqual(window.centralWidget().height(), height)
            self.assertTrue(window.start_button.isVisible())
            self.assertGreater(window.log_view.height(), 60)
            parameter_bottom = window.parameter_panel.mapTo(window, QPoint(0, window.parameter_panel.height())).y()
            agv_bottom = window.agv_panel.mapTo(window, QPoint(0, window.agv_panel.height())).y()
            self.assertLessEqual(abs(parameter_bottom - agv_bottom), 1)
            for widget in (window.scenario_combo, window.agv_labels["voltage"],
                           window.storage_grid.buttons[(6, 1)]):
                bottom = widget.mapTo(window, QPoint(0, widget.height())).y()
                self.assertLessEqual(bottom, window.vertical_splitter.widget(0).mapTo(window, QPoint(0, window.vertical_splitter.widget(0).height())).y())
            self.assertTrue(window.grab().save(str(Path("tmp") / f"gui-preview-{width}x{height}.png")))

    def test_triangle_buttons_are_visible_and_increment_decrement(self):
        window = self.window()
        self.assertNotIn("@ASSET_DIR@", self.app.styleSheet())
        self.assertIn("triangle-up.svg", self.app.styleSheet())
        for widget in (window.outbound_column, window.duration_input):
            widget.setValue(3)
            option = QStyleOptionSpinBox()
            widget.initStyleOption(option)
            up = widget.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                               QStyle.SubControl.SC_SpinBoxUp, widget)
            down = widget.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                                 QStyle.SubControl.SC_SpinBoxDown, widget)
            self.assertGreater(up.width(), 0)
            self.assertLess(up.center().y(), down.center().y())
            QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=up.center())
            self.assertGreater(widget.value(), 3)
            QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=down.center())
            self.assertEqual(widget.value(), 3)

    def test_real_start_preview_cancel_never_connects(self):
        window = self.window()
        window.mode_combo.setCurrentIndex(1)
        self.assertIn("未连接", window.agv_badge.text())
        self.assertFalse(window.scenario_combo.isEnabled())
        window.start_task()
        self.assertIsNotNone(window.real_start_dialog)
        self.assertFalse(window.controller.busy)
        self.assertFalse(window.parameter_panel.isEnabled())
        self.assertTrue(window.real_start_dialog.grab().save("tmp/gui-real-start-confirmation.png"))
        window.real_start_dialog.reject()
        self.assertIsNone(window.real_start_dialog)
        self.assertTrue(window.start_button.isEnabled())
        self.assertIn("未连接", window.agv_badge.text())
        window.mode_combo.setCurrentIndex(0)
        self.assertIn("开始模拟", window.start_button.text())

    def test_close_real_start_preview_does_not_start_worker(self):
        window = self.window()
        window.mode_combo.setCurrentIndex(1)
        window.start_task()
        window.close()
        self.assertFalse(window.isVisible())
        self.assertIsNone(window.real_start_dialog)
        self.assertIsNone(window.controller._thread)

    def real_window(self):
        from gui.real_backend import RealBackend
        from test_real_backend import ConnectedFakeAGV, MemoryModbus
        import warehouse_plc
        client, bus = ConnectedFakeAGV(profile="word"), MemoryModbus()
        factory = partial(RealBackend, agv_factory=lambda *a: client,
                          plc_factory=lambda *a, **kw: warehouse_plc.WarehousePLC(*a, **kw, client=bus))
        controller = TaskController(real_factory=factory)
        self.controllers.append(controller)
        self.store.save(replace(self.settings, plc_poll_interval=0.001, poll_interval=0.001))
        window = MainWindow(self.store, controller=controller)
        self.windows.append(window)
        window.resize(1366, 768)
        window.show()
        window.mode_combo.setCurrentIndex(1)
        return window, client, bus

    def test_real_gui_runs_original_flow_in_worker_with_fake_devices(self):
        from test_agv_task import FakeClock
        import outbound_agv
        import warehouse_plc
        window, client, bus = self.real_window()
        events = []
        window.controller.event.connect(events.append)
        clock = FakeClock()
        beats = []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(1))
        timer.start(2)
        with patch.object(outbound_agv, "time", clock), patch.object(warehouse_plc, "time", clock):
            window.start_task()
            self.assertIsNotNone(window.real_start_dialog)
            self.assertIsNone(client.socket)
            window.real_start_button.click()
            self.wait_until(lambda: window.confirmation_dialog is not None)
            QTest.qWait(30)
            self.assertTrue(window.controller.busy)
            self.assertIn("已连接", window.agv_badge.text())
            self.assertIn("真实确认", window.confirmation_dialog.windowTitle())
            window.confirm_button.click()
            self.wait_until(lambda: window.confirmation_dialog is not None)
            window.confirm_button.click()
            self.wait_until(lambda: not window.controller.busy)
        timer.stop()
        self.assertGreater(len(beats), 5)
        self.assertTrue(all(e.mode == "real" for e in events))
        self.assertEqual(window.stage_states, ["completed"] * 8)
        self.assertIn("真实完成", window.task_badge.text())
        self.assertIn("已断开", window.agv_badge.text())
        self.assertEqual(window.agv_labels["raw_station"].text(), "1")
        self.assertNotIn(threading.get_ident(), client.command_threads)
        self.assertTrue(window.start_button.isEnabled())
        self.assertFalse(window.scenario_combo.isEnabled())
        self.assertTrue(client.closed and bus.closed)
        self.assertTrue(window.grab().save("tmp/gui-real-completed-fake.png"))

    def test_real_gui_stop_during_confirmation_cleans_both_devices(self):
        window, client, bus = self.real_window()
        window.start_task()
        window.real_start_button.click()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        window.request_stop()
        self.wait_until(lambda: not window.controller.busy)
        self.assertEqual(window.controller.last_result.data["outcome"], "cancelled")
        self.assertNotIn((1, 1), client.commands)
        self.assertEqual(client.commands[-3:], [(2, 0), (23, 0), (6, 0)])
        self.assertTrue(client.closed and bus.closed)
        self.assertEqual(window.stage_states[1:7], ["waiting"] * 6)
        self.assertIsNone(window.confirmation_dialog)
        self.assertTrue(window.start_button.isEnabled())

    def test_real_gui_close_waits_for_cleanup_and_thread_exit(self):
        window, client, bus = self.real_window()
        window.start_task()
        window.real_start_button.click()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        window.close()
        self.wait_until(lambda: not window.controller.busy and not window.isVisible())
        self.assertTrue(client.closed and bus.closed)
        self.assertIsNone(window.controller._thread)

    def test_stop_button_exits_idle_without_creating_worker(self):
        window = self.window()
        QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
        self.assertFalse(window.isVisible())
        self.assertIsNone(window.controller._thread)

    def test_stop_button_exits_simulation_after_cleanup_and_thread_join(self):
        window = self.window()
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        # 确认窗口是模态窗口，用 click() 验证按钮信号连接。
        window.stop_button.click()
        self.assertTrue(window._closing)
        self.assertFalse(window.start_button.isEnabled())
        self.wait_until(lambda: not window.controller.busy and not window.isVisible())
        self.assertTrue(window.controller.last_result.data["cleanup_ok"])
        self.assertEqual(window.controller.last_result.data["outcome"], "cancelled")
        self.assertIsNone(window.controller._thread)

    def test_stop_button_during_real_infeed_clears_then_exits(self):
        from test_agv_task import FakeClock
        import outbound_agv
        import warehouse_plc
        window, client, bus = self.real_window()
        self.auto_confirm(window.controller)
        clock = FakeClock()
        entered = threading.Event()
        allow_read = threading.Event()
        original_read = bus.read_holding_registers
        def read(address, **kwargs):
            if address == 0 and bus.command == 3 and bus.busy_seen:
                entered.set()
                if not allow_read.wait(2):
                    raise TimeoutError("测试未释放入货反馈")
            result = original_read(address, **kwargs)
            if address == 0 and bus.command == 3:
                bus.busy_seen = True
            return result
        bus.read_holding_registers = read
        try:
            with patch.object(outbound_agv, "time", clock), patch.object(warehouse_plc, "time", clock):
                window.start_task()
                window.real_start_button.click()
                self.wait_until(entered.is_set)
                window.stop_button.click()
                self.assertTrue(window.isVisible())
                self.assertTrue(window.controller.busy)
                allow_read.set()
                self.wait_until(lambda: not window.controller.busy and not window.isVisible())
        finally:
            allow_read.set()
        self.assertEqual(window.controller.last_result.data["outcome"], "cancelled")
        self.assertTrue(window.controller.last_result.data["cleanup_ok"])
        self.assertEqual(bus.command, 0)
        self.assertNotIn(4, [v for a, v, t in bus.writes if a == 4 and v])
        self.assertTrue(client.closed and bus.closed)
        self.assertIsNone(window.controller._thread)

    def test_stop_button_exits_when_cleanup_fails_after_reporting_result(self):
        window = self.window()
        window.scenario_combo.setCurrentIndex(3)
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        window.stop_button.click()
        self.wait_until(lambda: not window.controller.busy and not window.isVisible())
        self.assertEqual(window.controller.last_result.data["outcome"], "failed")
        self.assertFalse(window.controller.last_result.data["cleanup_ok"])
        self.assertIsNone(window.controller._thread)

    def test_control_rejects_stale_answers_and_cancel_wakes_wait(self):
        control = TaskControl()
        control.prepare_confirmation(2)
        self.assertFalse(control.answer(1, True))
        self.assertTrue(control.answer(2, True))
        control.wait_confirmation(2, 1)
        self.assertFalse(control.answer(2, True))
        errors = []
        def wait():
            try:
                control.wait(10)
            except TaskCancelled:
                errors.append("cancelled")
        thread = threading.Thread(target=wait)
        thread.start()
        control.cancel()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])

    def test_running_monitor_and_confirmation_render_correctly(self):
        window = self.window(time_scale=0.04)
        window.resize(1366, 768)
        window.start_task()
        self.wait_until(lambda: window.confirmation_dialog is not None)
        self.assertTrue(window.confirmation_dialog.grab().save("tmp/gui-confirmation.png"))
        QTest.mouseClick(window.confirm_button, Qt.MouseButton.LeftButton)
        self.wait_until(lambda: window.confirmation_dialog is not None)
        self.assertEqual(window.stage_states[2], "running")
        self.assertIn("等待上料", window.stage_rows[2][1].text())
        QTest.mouseClick(window.confirm_button, Qt.MouseButton.LeftButton)
        self.wait_until(lambda: window.stage_states[3] == "running")
        self.wait_until(lambda: window.device_values.get("in_transit"))
        self.assertIn("上次确认", window.agv_labels["confirmed_station"].text())
        self.assertIn("前往站点 2", window.route_label.text())
        self.assertIn("35%", window.agv_labels["speed_percent"].text())
        self.assertTrue(window.agv_icon.moving)
        self.assertFalse(window.station1_label.property("confirmed"))
        self.assertFalse(window.station2_label.property("confirmed"))
        self.assertTrue(window.grab().save("tmp/gui-running.png"))
        self.wait_until(lambda: not window.controller.busy)
        self.assertTrue(window.grab().save("tmp/gui-completed.png"))

    def test_worker_cleans_up_when_backend_raises_unexpected_exception(self):
        class BrokenBackend:
            def __init__(self, *args, **kwargs):
                pass
            def run(self):
                raise RuntimeError("模拟执行器异常")
            def cleanup(self):
                cleaned.append(True)
        cleaned, events = [], []
        worker = TaskWorker(self.parameters, self.settings, TaskControl(), BrokenBackend, {})
        worker.event.connect(events.append)
        worker.run()
        self.assertEqual(cleaned, [True])
        self.assertEqual(events[-1].data["outcome"], "failed")
        self.assertTrue(events[-1].data["cleanup_ok"])


if __name__ == "__main__":
    Path("tmp").mkdir(exist_ok=True)
    unittest.main(verbosity=2)
