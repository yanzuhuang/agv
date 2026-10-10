"""验证 Windows 原生 Qt 的停止按钮使事件循环正常退出；禁止设备连接。"""
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
from gui.models import EventKind
from gui.settings import DeviceSettings, SettingsStore


app = create_application()
app.setQuitOnLastWindowClosed(True)
with TemporaryDirectory(dir=TMP_DIR) as folder, ExitStack() as stack:
    guards = [stack.enter_context(patch(name, side_effect=AssertionError(name)))
              for name in ("socket.create_connection", "socket.socket",
                           "pymodbus.client.ModbusTcpClient.connect", "builtins.input")]
    store = SettingsStore(Path(folder) / "settings.ini")
    store.save(DeviceSettings(roller_profile="word"))
    window = MainWindow(store, time_scale=0.05)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window._show_confirmation = lambda request: window.controller.answer_confirmation(request["request_id"], True)
    clicked = []
    def event(event):
        if event.kind == EventKind.STAGE and event.data["stage"] == 3 and event.data["status"] == "running":
            clicked.append(True)
            window.stop_button.click()
    window.controller.event.connect(event)
    window.show()
    QTimer.singleShot(0, window.start_task)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(window.stop_and_exit)
    watchdog.start(8000)
    code = app.exec()  # 停止后由窗口关闭自然退出，不调用 app.quit()。
    assert code == 0 and clicked
    assert not window.isVisible() and not window.controller.busy
    assert window.controller._thread is None
    assert window.controller.last_result.data["outcome"] == "cancelled"
    assert window.controller.last_result.data["cleanup_ok"]
    for guard in guards:
        guard.assert_not_called()
    print("Windows stop/exit smoke passed: cleanup, thread joined, event loop exited naturally.")
