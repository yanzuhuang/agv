"""Windows Qt 平台冒烟验证；不显示交互窗口，禁止现场设备通信。"""
import os
os.environ["QT_QPA_PLATFORM"] = "windows"
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
TMP_DIR = PROJECT_ROOT / 'tmp'
TMP_DIR.mkdir(exist_ok=True)
from tempfile import TemporaryDirectory
from unittest.mock import patch
from contextlib import ExitStack

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QRawFont
from gui.main import create_application
from gui.main_window import MainWindow
from gui.models import EventKind
from gui.settings import DeviceSettings, SettingsStore


app = create_application()
assert app.platformName() == "windows"
assert QRawFont.fromFont(app.font()).supportsCharacter(ord("仓"))
app.setQuitOnLastWindowClosed(False)
with TemporaryDirectory(dir=TMP_DIR) as folder, ExitStack() as stack:
    guards = [stack.enter_context(patch(name, side_effect=AssertionError(name)))
              for name in ("socket.create_connection", "socket.socket",
                           "pymodbus.client.ModbusTcpClient.connect", "builtins.input")]
    store = SettingsStore(Path(folder) / "settings.ini")
    store.save(DeviceSettings(roller_profile="word"))
    window = MainWindow(store, time_scale=0.005)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(1366, 768)
    beats, results = [], []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: beats.append(1))
    heartbeat.start(5)
    def event(event):
        if event.kind == EventKind.CONFIRMATION:
            window.controller.answer_confirmation(event.data["request_id"], True)
    def finished(result):
        results.append(result)
        assert result.data["outcome"] == "success"
        assert window.grab().save(str(TMP_DIR / "gui-native-windows-phase2.png"))
        window.close()
        app.quit()
    window.controller.event.connect(event)
    window.controller.task_finished.connect(finished)
    window.show()
    QTimer.singleShot(0, window.start_task)
    def timeout():
        window.close()
        if not window.controller.busy:
            app.quit()
    QTimer.singleShot(8000, timeout)
    app.exec()
    assert results and len(beats) > 5
    assert not window.controller.busy
    for guard in guards:
        guard.assert_not_called()
    print("Windows Qt smoke passed: complete simulation, triangles, Chinese font, no device connections.")
