"""运行：python -m gui.main"""

from pathlib import Path
import os
import sys

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def create_application():
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("WarehouseAGV")
    application.setOrganizationName("WarehouseAGV")
    application.setStyle("Fusion")
    # Windows offscreen 插件不枚举系统字体；从已安装字体加载中文支持。
    # 正常桌面模式沿用系统字体，不随应用分发字体文件。
    if os.name == "nt" and not QFontDatabase.families():
        font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            font_path = font_dir / filename
            if font_path.is_file():
                QFontDatabase.addApplicationFont(str(font_path))
    application.setFont(QFont("Microsoft YaHei UI", 9))
    style = Path(__file__).with_name("styles.qss").read_text(encoding="utf-8")
    style = style.replace("@ASSET_DIR@", Path(__file__).with_name("assets").as_posix())
    application.setStyleSheet(style)
    return application


def main():
    application = create_application()
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
