"""Windows 原生 Qt 单体模拟冒烟验证，不连接现场设备。"""
import os
os.environ["QT_QPA_PLATFORM"] = "windows"
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
TMP_DIR = PROJECT_ROOT / 'tmp'
TMP_DIR.mkdir(exist_ok=True)
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from gui.main import create_application
from gui.main_window import MainWindow
from gui.settings import SettingsStore

app = create_application()
app.setQuitOnLastWindowClosed(False)
with TemporaryDirectory(dir=TMP_DIR) as folder, ExitStack() as stack:
    guards = [stack.enter_context(patch(name, side_effect=AssertionError(name)))
              for name in ("socket.create_connection", "socket.socket", "builtins.input",
                           "pymodbus.client.ModbusTcpClient.connect")]
    window = MainWindow(SettingsStore(Path(folder) / "settings.ini"), time_scale=0.01)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(1366, 768)
    window.task_type_combo.setCurrentIndex(1)
    assert window.agv_target_input.value() == 1
    window.agv_target_input.setValue(7)
    window.agv_direction_combo.setCurrentIndex(1)
    window._show_confirmation = lambda request: window.controller.answer_confirmation(request["request_id"], True)
    results = []
    def finished(result):
        results.append(result)
        window.grab().save(str(TMP_DIR / "gui-agv-single-native.png"))
        window.close()
        app.quit()
    window.controller.task_finished.connect(finished)
    window.show()
    QTimer.singleShot(0, window.start_task)
    app.exec()
    assert results[0].data["outcome"] == "success"
    assert results[0].task_type == "agv_move"
    assert window.stage_states == ["completed"] * 4
    assert window.agv_labels["raw_station"].text() == "7"
    assert window.controller._thread is None
    for guard in guards:
        guard.assert_not_called()
    print("Windows AGV single smoke passed: default station 1, four stages, complete simulation, no device connections.")
