"""界面和执行器共用的数据契约，不包含设备通信。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import math


STAGES = (
    "准备与设备检查", "仓库出库", "线体与 AGV 上料", "AGV 前往站点 2",
    "AGV 返回站点 1", "线体与 AGV 下料", "仓库入库", "完成与清理",
)


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


@dataclass(frozen=True)
class TaskParameters:
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
