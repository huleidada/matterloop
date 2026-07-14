"""显式生命周期约束的单元测试。"""

import pytest
from matterloop_core import InvalidStateTransitionError, LoopStatus, ensure_transition


def test_valid_transition_is_accepted() -> None:
    """正常的首次状态转换应当合法。"""
    ensure_transition(LoopStatus.CREATED, LoopStatus.PLANNING)


def test_terminal_state_cannot_restart() -> None:
    """已完成的运行不得被静默重新启动。"""
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(LoopStatus.COMPLETED, LoopStatus.PLANNING)
