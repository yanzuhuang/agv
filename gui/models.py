"""界面和执行器共用的数据契约，不包含设备通信。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import math
from typing import ClassVar


STAGES = (
    "准备与设备检查", "仓库出库", "线体与 AGV 上料", "AGV 前往站点 2",
    "AGV 返回站点 1", "线体与 AGV 下料", "仓库入库", "完成与清理",
)
AGV_STAGES = ("AGV 检查与准备", "前往指定站点", "到站停车确认", "完成与清理")


class EventKind(str, Enum):
    STAGE = "stage"
    DEVICE = "device"
    LOG = "log"
    CONFIRMATION = "confirmation"
    CLEANUP = "cleanup"
    RESULT = "result"


@dataclass(frozen=True)
class TaskEvent:
    kind: EventKind
    data: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    mode: str = "simulation"
    task_type: str = "warehouse"


@dataclass(frozen=True)
class TaskParameters:
    stages: ClassVar[tuple] = STAGES
    task_type: ClassVar[str] = "warehouse"
    column: int
    level: int
    inbound_column: int
    inbound_level: int
    duration: float

    def validate(self, settings):
        settings.validate(require_profile=True)
        for name, maximum in (("column", 6), ("level", 3),
                              ("inbound_column", 6), ("inbound_level", 3)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("出入库列号须为 1～6，层号须为 1～3。")
        if (not isinstance(self.duration, (int, float))
                or not math.isfinite(self.duration)
                or not 2 < self.duration <= settings.action_timeout):
            raise ValueError("线体出货运行时间须大于 2 秒，且不超过动作超时。")


@dataclass(frozen=True)
class AgvMoveParameters:
    """站点 0 是清目标指令，不能作为行走目标；方向由现场路线决定。"""
    target_station: int = 1
    direction: str = "forward"
    stages: ClassVar[tuple] = AGV_STAGES
    task_type: ClassVar[str] = "agv_move"

    @property
    def direction_text(self):
        return "前进" if self.direction == "forward" else "后退"

    def validate(self, settings):
        settings.validate()
        if type(self.target_station) is not int or not 1 <= self.target_station <= 255:
            raise ValueError("AGV 目标站点须为 1～255；0 用于清除目标。")
        if self.direction not in ("forward", "backward"):
            raise ValueError("AGV 行走方向须选择前进或后退。")
