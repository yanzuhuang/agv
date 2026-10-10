"""源码和 PyInstaller 共用的 GUI 启动入口。"""

from gui.main import main


if __name__ == "__main__":
    raise SystemExit(main())
