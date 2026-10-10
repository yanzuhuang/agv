"""A 库任务位置选择器；不表达库位占用。"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QWidget


def refresh_style(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class StorageGrid(QWidget):
    selected = Signal(str, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection_mode = "outbound"
        self.outbound = (6, 1)
        self.inbound = (6, 1)
        self.buttons = {}
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        for row, level in enumerate((3, 2, 1)):
            layout.addWidget(QLabel(f"{level} 层"), row, 0)
            for column in range(1, 7):
                button = QPushButton()
                button.setObjectName(f"slot_{column}_{level}")
                button.setMinimumSize(42, 40)
                button.clicked.connect(lambda checked=False, c=column, l=level: self._clicked(c, l))
                self.buttons[(column, level)] = button
                layout.addWidget(button, row, column)
        for column in range(1, 7):
            label = QLabel(f"{column} 列")
            label.setObjectName("mutedLabel")
            layout.addWidget(label, 3, column)
        self.set_selection(self.outbound, self.inbound)

    def set_mode(self, mode):
        if mode not in ("outbound", "inbound"):
            raise ValueError("未知的库位选择方式。")
        self.selection_mode = mode

    def _clicked(self, column, level):
        self.selected.emit(self.selection_mode, column, level)

    def set_selection(self, outbound, inbound):
        self.outbound, self.inbound = outbound, inbound
        for position, button in self.buttons.items():
            out, inc = position == outbound, position == inbound
            choice = "both" if out and inc else "outbound" if out else "inbound" if inc else "none"
            suffix = "出 / 入" if out and inc else "出库" if out else "入库" if inc else ""
            button.setText(f"{position[0]}-{position[1]}" + (f"\n{suffix}" if suffix else ""))
            button.setProperty("selection", choice)
            button.setToolTip(f"A 库 {position[1]} 层 {position[0]} 列；仅选择任务位置，不表示库存")
            refresh_style(button)
