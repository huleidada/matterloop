"""MatterLoop 预设装配阶段的类型化异常。"""


class PresetError(Exception):
    """所有预设装配异常的基类。"""


class PresetConfigurationError(PresetError, ValueError):
    """预设缺少安全运行所需显式依赖时抛出。"""


__all__ = ["PresetConfigurationError", "PresetError"]
