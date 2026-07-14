"""基于反馈收敛情况的停止策略。"""

from dataclasses import dataclass

from matterloop_core import LoopContext


@dataclass(frozen=True, slots=True)
class StopConfig:
    """配置允许重复出现相同失败反馈的次数。"""

    max_identical_feedback: int = 2

    def __post_init__(self) -> None:
        """保证停止阈值至少允许一次反馈。"""
        if self.max_identical_feedback < 1:
            raise ValueError("max_identical_feedback must be at least 1")


class NoProgressStopPolicy:
    """重复收到相同验证反馈时阻止无效循环。"""

    def __init__(self, config: StopConfig | None = None) -> None:
        self._config = config or StopConfig()

    def can_continue(self, context: LoopContext) -> bool:
        """判断最近失败反馈是否仍有变化。"""
        feedback = [
            record.verification.feedback
            for record in context.records
            if not record.verification.passed and record.verification.feedback
        ]
        window = feedback[-self._config.max_identical_feedback :]
        return not (len(window) == self._config.max_identical_feedback and len(set(window)) == 1)
