"""MatterLoop 开箱即用预设的公共 API。"""

from matterloop_presets.coding import build_coding_local_runtime, build_coding_runtime
from matterloop_presets.config import (
    AgentPresetConfig,
    CodingPresetConfig,
    MinimalPresetConfig,
    ProductionPresetConfig,
    ResearchPresetConfig,
)
from matterloop_presets.errors import PresetConfigurationError, PresetError
from matterloop_presets.minimal import build_minimal_local_runtime, build_minimal_runtime
from matterloop_presets.production import (
    build_production_local_runtime,
    build_production_runtime,
)
from matterloop_presets.research import (
    build_research_local_runtime,
    build_research_runtime,
)
from matterloop_presets.runtime import PresetRuntime, ProductionLocalRuntime, ProductionRuntime

__all__ = [
    "AgentPresetConfig",
    "CodingPresetConfig",
    "MinimalPresetConfig",
    "PresetConfigurationError",
    "PresetError",
    "PresetRuntime",
    "ProductionLocalRuntime",
    "ProductionPresetConfig",
    "ProductionRuntime",
    "ResearchPresetConfig",
    "build_coding_local_runtime",
    "build_coding_runtime",
    "build_minimal_local_runtime",
    "build_minimal_runtime",
    "build_production_local_runtime",
    "build_production_runtime",
    "build_research_local_runtime",
    "build_research_runtime",
]
