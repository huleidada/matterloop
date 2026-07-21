"""公开字段、文档链接和离线边界的工作区级契约测试。"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import re
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]

# 这些类型构成用户确认的“集成公共面”。私有实现和 continuation 内部载荷不进入字段文档。
FIELD_CONTRACTS: dict[str, dict[str, tuple[str, ...]]] = {
    "matterloop-core/README.md": {
        "matterloop_core": (
            "ArtifactRef",
            "CompletionDecision",
            "ComponentSpec",
            "ExecutionResult",
            "HumanInteractionRecord",
            "HumanInteractionRequest",
            "HumanResponse",
            "IterationRecord",
            "LoopContext",
            "LoopEvent",
            "LoopLimits",
            "LoopRequest",
            "LoopResult",
            "Plan",
            "PlanStep",
            "PluginDefinition",
            "RetryDecision",
            "VerificationResult",
        ),
    },
    "matterloop-models/README.md": {
        "matterloop_models": (
            "ModelCapabilities",
            "ModelDescriptor",
            "ModelMessage",
            "ModelRequest",
            "ModelRequirements",
            "ModelResponse",
            "TokenUsage",
            "ToolCall",
            "ToolDefinition",
            "ToolOutput",
        ),
        "matterloop_models.providers": (
            "DeepSeekModelConfig",
            "GLMModelConfig",
            "MiniMaxModelConfig",
            "OpenAICompatibleChatConfig",
            "OpenAIModelConfig",
            "QwenModelConfig",
            "ZhipuModelConfig",
        ),
    },
    "matterloop-runtime/README.md": {
        "matterloop_runtime": (
            "ProcessRequest",
            "ProcessResult",
            "QueueLease",
            "QueuedRun",
            "RunRecord",
        ),
    },
    "matterloop-tools/README.md": {
        "matterloop_tools": (
            "McpCallResult",
            "McpCatalog",
            "McpContent",
            "McpLimits",
            "McpPromptArgument",
            "McpPromptDefinition",
            "McpPromptMessage",
            "McpPromptResult",
            "McpResourceDefinition",
            "McpResourceResult",
            "McpResourceTemplateDefinition",
            "McpServerCapabilities",
            "McpServerConfig",
            "McpToolDefinition",
            "SkillAccessPolicy",
            "SkillContent",
            "SkillContextBlock",
            "SkillLoaderConfig",
            "SkillSpec",
            "ToolContext",
            "ToolResult",
            "ToolSpec",
        ),
    },
    "matterloop-memory/README.md": {
        "matterloop_memory": ("MemoryMatch", "MemoryQuery", "MemoryRecord"),
    },
    "matterloop-policies/README.md": {
        "matterloop_policies": (
            "ApprovalRule",
            "BudgetLimits",
            "PermissionRule",
            "RetryConfig",
            "StopConfig",
            "TokenRateCard",
            "UsageAmount",
            "UsageReservation",
            "UsageSnapshot",
        ),
    },
    "matterloop-agents/README.md": {
        "matterloop_agents": (
            "CriteriaVerifierConfig",
            "ModelPlannerConfig",
            "ModelReviewerConfig",
            "ReviewResult",
            "ToolCallingWorkerConfig",
        ),
        "matterloop_agents.collaboration": (
            "AgentMessage",
            "AgentSpec",
            "AgentTaskContext",
            "ModelResultAggregatorConfig",
            "ModelTaskVerifierConfig",
            "ModelTeamPlannerConfig",
            "ModelTeamReviewerConfig",
            "TaskResult",
            "TaskSpec",
            "TaskState",
            "TaskVerification",
            "TeamCycleRecord",
            "TeamEvent",
            "TeamLimits",
            "TeamOrchestratorComponents",
            "TeamPlanningContext",
            "TeamRequest",
            "TeamResult",
            "TeamReview",
            "TeamReviewContext",
            "TeamSnapshot",
        ),
    },
    "matterloop-presets/README.md": {
        "matterloop_presets": (
            "AgentPresetConfig",
            "CodingPresetConfig",
            "MinimalPresetConfig",
            "ProductionPresetConfig",
            "ResearchPresetConfig",
        ),
    },
    "matterloop-integration-fastapi/README.md": {
        "matterloop_integration_fastapi": (
            "ArtifactResponse",
            "CancelResponse",
            "CreateLoopRequest",
            "EventListResponse",
            "ExecutionResponse",
            "IterationResponse",
            "LoopLimitsRequest",
            "PlanStepResponse",
            "ResumeLoopRequest",
            "ResumeResponse",
            "RunResponse",
            "VerificationResponse",
        ),
    },
    "matterloop-integration-celery/README.md": {
        "matterloop_integration_celery": (
            "CeleryWorkerDependencies",
            "RegisteredCeleryTasks",
        ),
    },
    "matterloop-integration-redis/README.md": {
        "matterloop_integration_redis": ("RedisConfig",),
    },
}

SIGNATURE_CONTRACTS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "matterloop-core/README.md": (
        (
            "matterloop_core",
            "AgentLoop",
            (
                "planners",
                "executors",
                "verifiers",
                "checkpoint_store",
                "policy",
                "events",
                "approval_gate",
                "retry_policy",
                "completion_evaluator",
            ),
        ),
    ),
    "matterloop-models/README.md": (
        (
            "matterloop_models",
            "ModelRegistry.register",
            ("name", "client", "replace", "descriptor"),
        ),
        ("matterloop_models", "ModelRegistry.acquire", ("name",)),
        ("matterloop_models", "ModelRegistry.swap", ("name", "client", "descriptor")),
    ),
    "matterloop-runtime/README.md": (
        ("matterloop_runtime", "AsyncRuntime", ("engine", "resources")),
        ("matterloop_runtime", "LocalRuntime", ("runtime", "thread_name")),
        ("matterloop_runtime", "QueueRuntime", ("producer", "repository", "event_reader")),
    ),
    "matterloop-tools/README.md": (
        ("matterloop_tools", "ToolRegistry", ("tools", "authorizer")),
        (
            "matterloop_tools",
            "McpServerConnection",
            ("session", "config", "mapper"),
        ),
        (
            "matterloop_tools",
            "McpSdkV1SessionAdapter",
            ("session", "close_callback"),
        ),
        ("matterloop_tools", "SkillLoader", ("config",)),
        ("matterloop_tools", "SkillRegistry", ("skills",)),
        (
            "matterloop_tools",
            "SkillContextAdapter",
            ("registry", "policy"),
        ),
        ("matterloop_tools", "SkillTool", ("adapter", "name")),
        (
            "matterloop_tools",
            "FileSystemTool",
            ("root", "allow_write", "max_read_bytes", "max_write_bytes", "max_list_entries"),
        ),
        (
            "matterloop_tools",
            "ShellTool",
            (
                "workspace",
                "allowed_commands",
                "sandbox",
                "base_environment",
                "allowed_environment",
                "max_timeout_seconds",
                "max_output_bytes",
            ),
        ),
        (
            "matterloop_tools",
            "HttpTool",
            (
                "allowed_hosts",
                "allowed_methods",
                "require_https",
                "follow_redirects",
                "max_redirects",
                "max_timeout_seconds",
                "max_response_bytes",
                "max_request_bytes",
                "allowed_headers",
                "transport",
            ),
        ),
    ),
    "matterloop-observability/README.md": (
        ("matterloop_observability", "CompositeEventPublisher", ("publishers", "failure_mode")),
        ("matterloop_observability", "StructuredLoggingHandler", ("logger", "redactor")),
        ("matterloop_observability", "Redactor", ("extra_fields",)),
    ),
    "matterloop-presets/README.md": (
        ("matterloop_presets", "build_minimal_runtime", ("model", "config")),
        ("matterloop_presets", "build_coding_runtime", ("model", "workspace", "config")),
        (
            "matterloop_presets",
            "build_research_runtime",
            ("model", "workspace", "config"),
        ),
        (
            "matterloop_presets",
            "build_production_runtime",
            (
                "model",
                "config",
                "queue_backend",
                "run_repository",
                "checkpoint_store",
                "audit_publisher",
                "event_reader",
                "approval_gate",
            ),
        ),
    ),
    "matterloop-integration-fastapi/README.md": (
        (
            "matterloop_integration_fastapi",
            "create_router",
            ("runtime", "auth_dependency", "prefix"),
        ),
    ),
    "matterloop-integration-celery/README.md": (
        ("matterloop_integration_celery", "CeleryQueueProducer", ("app", "queue", "codec")),
        ("matterloop_integration_celery", "register_tasks", ("celery_app", "runtime_factory_path")),
    ),
    "matterloop-integration-redis/README.md": (
        (
            "matterloop_integration_redis",
            "RedisCheckpointStore",
            ("client", "config", "codec"),
        ),
        ("matterloop_integration_redis", "RedisQueueBackend", ("client", "config", "codec")),
        ("matterloop_integration_redis", "RedisRunRepository", ("client", "config", "codec")),
        (
            "matterloop_integration_redis",
            "RedisEventPublisher",
            ("client", "config", "checkpoint_codec"),
        ),
    ),
}


def test_integration_public_fields_are_documented() -> None:
    """字段契约中的每个真实字段都必须出现在中英文 README 的代码标记中。"""
    for readme_path, modules in FIELD_CONTRACTS.items():
        for localized_path in _localized_paths(ROOT / readme_path):
            document = localized_path.read_text(encoding="utf-8")
            for module_name, class_names in modules.items():
                module = importlib.import_module(module_name)
                for class_name in class_names:
                    value = getattr(module, class_name)
                    assert _inside_code_span(document, class_name), (
                        f"{localized_path} 没有说明公共类型 {module_name}.{class_name}"
                    )
                    for field_name in _public_fields(value):
                        assert _inside_code_span(document, field_name), (
                            f"{localized_path} 没有说明 {class_name}.{field_name}"
                        )


def test_integration_entrypoint_parameters_are_documented_and_real() -> None:
    """关键构造器参数必须同时存在于代码签名和中英文文档。"""
    for readme_path, contracts in SIGNATURE_CONTRACTS.items():
        for localized_path in _localized_paths(ROOT / readme_path):
            document = localized_path.read_text(encoding="utf-8")
            for module_name, qualified_name, parameter_names in contracts:
                value = _resolve(importlib.import_module(module_name), qualified_name)
                signature = inspect.signature(value)
                for parameter_name in parameter_names:
                    assert parameter_name in signature.parameters, (
                        f"契约参数已过期：{module_name}.{qualified_name}.{parameter_name}"
                    )
                    assert _inside_code_span(document, parameter_name), (
                        f"{localized_path} 没有说明 {qualified_name}.{parameter_name}"
                    )


def test_public_markdown_documents_have_english_mirrors() -> None:
    """每份公开中文文档都必须存在结构完整且可双向切换的英文镜像。"""
    for primary_path in _primary_documents():
        english_path = _english_mirror(primary_path)
        assert english_path.is_file(), f"缺少英文镜像：{english_path}"

        primary = primary_path.read_text(encoding="utf-8")
        english = english_path.read_text(encoding="utf-8")
        expected_primary_switch, expected_english_switch = _expected_language_switches(
            primary_path, english_path
        )
        assert _first_content_line(primary) == expected_primary_switch, (
            f"{primary_path} 的语言入口不正确"
        )
        assert _first_content_line(english) == expected_english_switch, (
            f"{english_path} 的语言入口不正确"
        )
        assert _document_structure(primary) == _document_structure(english), (
            f"{english_path} 的 Markdown 结构与中文原文不一致"
        )


def test_english_documents_prefer_english_internal_links() -> None:
    """英文页面不得在正文中把用户带回已有英文镜像的中文页面。"""
    primary_documents = {path.resolve() for path in _primary_documents()}
    link_pattern = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    github_pattern = re.compile(
        r"^https://github\.com/huleidada/matterloop/blob/main/(?P<path>[^#?]+\.md)"
    )
    for primary_path in _primary_documents():
        english_path = _english_mirror(primary_path)
        english = english_path.read_text(encoding="utf-8")
        prose = _without_fenced_code_blocks(_without_first_content_line(english))
        for raw_target in link_pattern.findall(prose):
            target = raw_target.strip().strip("<>")
            github_match = github_pattern.match(target)
            if github_match is not None:
                linked_path = (ROOT / unquote(github_match.group("path"))).resolve()
                assert linked_path not in primary_documents, (
                    f"{english_path} 应链接英文镜像而不是 {raw_target}"
                )
                continue
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target_path = unquote(target.split("#", maxsplit=1)[0])
            resolved = (english_path.parent / target_path).resolve()
            assert resolved not in primary_documents, (
                f"{english_path} 应链接英文镜像而不是 {raw_target}"
            )
            assert not (resolved.is_dir() and (resolved / "README.en.md").is_file()), (
                f"{english_path} 应直接链接目录中的 README.en.md：{raw_target}"
            )


def test_english_documents_do_not_leave_untranslated_chinese() -> None:
    """除语言切换示例外，英文镜像不得残留中文正文或示例输入。"""
    chinese_pattern = re.compile(r"[\u3400-\u9fff]")
    for primary_path in _primary_documents():
        english_path = _english_mirror(primary_path)
        english = _without_first_content_line(english_path.read_text(encoding="utf-8"))
        if english_path == ROOT / "docs" / "i18n.en.md":
            english = _without_fenced_code_blocks(english)
        match = chinese_pattern.search(english)
        assert match is None, f"{english_path} 残留未翻译中文：{match.group(0) if match else ''}"


def test_markdown_internal_links_resolve() -> None:
    """所有中英文公开文档的仓库内部链接与锚点必须存在。"""
    documents = tuple(
        path
        for primary_path in _primary_documents()
        for path in (primary_path, _english_mirror(primary_path))
    )
    link_pattern = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    repository_pattern = re.compile(
        r"^https://github\.com/huleidada/matterloop/(?:blob|tree)/main/"
        r"(?P<path>[^#?]+)(?:#(?P<fragment>[^?]+))?$"
    )
    for document_path in documents:
        document = document_path.read_text(encoding="utf-8")
        prose = _without_fenced_code_blocks(document)
        for raw_target in link_pattern.findall(prose):
            target = raw_target.strip().strip("<>")
            repository_match = repository_pattern.match(target)
            if repository_match is not None:
                resolved = (ROOT / unquote(repository_match.group("path"))).resolve()
                assert resolved.exists(), f"{document_path} 包含失效链接：{raw_target}"
                fragment = repository_match.group("fragment")
                if fragment and resolved.suffix == ".md":
                    _assert_markdown_anchor(document_path, fragment, resolved)
                continue
            if target.startswith(("#", "http://", "https://", "mailto:")):
                if target.startswith("#"):
                    _assert_markdown_anchor(document_path, target[1:], document_path)
                continue
            target_path, _, fragment = target.partition("#")
            target_path = unquote(target_path)
            resolved = (document_path.parent / target_path).resolve()
            assert resolved.exists(), f"{document_path} 包含失效链接：{raw_target}"
            if fragment and resolved.suffix == ".md":
                _assert_markdown_anchor(document_path, fragment, resolved)


def test_examples_do_not_read_environment_or_contain_key_material() -> None:
    """离线示例不得读取环境配置，也不得包含常见密钥前缀。"""
    forbidden_patterns = (
        re.compile(r"os\.environ"),
        re.compile(r"os\.getenv"),
        re.compile(r"load_dotenv"),
        re.compile(r"dotenv_values"),
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}"),
    )
    for example_path in (ROOT / "examples" / "enterprise").glob("*.py"):
        source = example_path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert pattern.search(source) is None, f"{example_path} 包含禁止内容：{pattern.pattern}"


def _public_fields(value: object) -> tuple[str, ...]:
    """读取 dataclass 或 Pydantic 公共字段名称。"""
    if inspect.isclass(value) and dataclasses.is_dataclass(value):
        return tuple(field.name for field in dataclasses.fields(value))
    if inspect.isclass(value) and issubclass(value, BaseModel):
        return tuple(value.model_fields)
    raise TypeError(f"unsupported documentation contract type: {value!r}")


def _primary_documents() -> tuple[Path, ...]:
    """返回需要维护英文镜像的公开中文 Markdown。"""
    roots = (ROOT / "docs", ROOT / "examples", *(ROOT.glob("matterloop-*")))
    documents = {path for path in ROOT.glob("*.md") if not path.name.endswith(".en.md")}
    for document_root in roots:
        documents.update(
            path
            for path in document_root.rglob("*.md")
            if not path.name.endswith(".en.md") and not _is_generated_document(path)
        )
    return tuple(sorted(documents))


def _english_mirror(path: Path) -> Path:
    """按工作区约定计算英文镜像路径。"""
    if path.name == "README.md":
        return path.with_name("README.en.md")
    return path.with_name(f"{path.stem}.en.md")


def _localized_paths(path: Path) -> tuple[Path, Path]:
    """返回中文原文和英文镜像。"""
    return path, _english_mirror(path)


def _expected_language_switches(primary_path: Path, english_path: Path) -> tuple[str, str]:
    """返回中文与英文页面应使用的精确语言入口。"""
    if primary_path.parent.name.startswith("matterloop-"):
        base = f"https://github.com/huleidada/matterloop/blob/main/{primary_path.parent.name}/"
        return (
            f"简体中文 | [English]({base}{english_path.name})",
            f"[简体中文]({base}{primary_path.name}) | English",
        )
    return (
        f"简体中文 | [English]({english_path.name})",
        f"[简体中文]({primary_path.name}) | English",
    )


def _document_structure(document: str) -> tuple[object, ...]:
    """提取翻译必须保持一致的 Markdown 结构。"""
    return (
        tuple(len(match.group(1)) for match in re.finditer(r"^(#{1,6})\s", document, re.MULTILINE)),
        tuple(re.findall(r"^```([^\n]*)$", document, re.MULTILINE)),
        len(re.findall(r"^\s*[-*+]\s+", document, re.MULTILINE)),
        len(re.findall(r"^\s*\d+\.\s+", document, re.MULTILINE)),
        len(re.findall(r"^\s*[-*+]\s+\[[ xX]\]\s+", document, re.MULTILINE)),
        len(re.findall(r"^\s*\|.*\|\s*$", document, re.MULTILINE)),
        document.count("<details>"),
        document.count("</details>"),
    )


def _without_fenced_code_blocks(document: str) -> str:
    """移除代码块，避免把示例中的 Markdown 字面量当成真实链接。"""
    return re.sub(r"```.*?```", "", document, flags=re.DOTALL)


def _first_content_line(document: str) -> str:
    """返回文档首个非空行。"""
    return next(line.strip() for line in document.splitlines() if line.strip())


def _without_first_content_line(document: str) -> str:
    """移除语言切换行，供英文正文检查使用。"""
    lines = document.splitlines()
    first_index = next(index for index, line in enumerate(lines) if line.strip())
    return "\n".join((*lines[:first_index], *lines[first_index + 1 :]))


def _is_generated_document(path: Path) -> bool:
    """排除构建、缓存和虚拟环境中的 Markdown。"""
    generated_directories = {".pytest_cache", ".venv", "build", "dist"}
    return any(part in generated_directories for part in path.parts)


def _assert_markdown_anchor(source: Path, fragment: str, target: Path) -> None:
    """确认相对 Markdown 链接中的 GitHub 风格锚点存在。"""
    decoded_fragment = unquote(fragment).lower()
    anchors = _markdown_anchors(target.read_text(encoding="utf-8"))
    assert decoded_fragment in anchors, f"{source} 包含失效锚点：{target}#{fragment}"


def _markdown_anchors(document: str) -> set[str]:
    """为文档标题生成当前契约所需的 GitHub 风格锚点。"""
    prose = _without_fenced_code_blocks(document)
    anchors: set[str] = set()
    duplicates: dict[str, int] = {}
    for match in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", prose, re.MULTILINE):
        heading = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", match.group(1))
        heading = heading.replace("`", "").lower()
        base = re.sub(r"[^\w\- ]", "", heading, flags=re.UNICODE).replace("_", "")
        base = re.sub(r"\s+", "-", base.strip())
        duplicate = duplicates.get(base, 0)
        anchors.add(base if duplicate == 0 else f"{base}-{duplicate}")
        duplicates[base] = duplicate + 1
    return anchors


def _inside_code_span(document: str, name: str) -> bool:
    """判断名称是否出现在 Markdown 行内代码标记中。"""
    pattern = re.compile(rf"`[^`\n]*\b{re.escape(name)}\b[^`\n]*`")
    return pattern.search(document) is not None


def _resolve(module: ModuleType, qualified_name: str) -> object:
    """从公共模块解析类、函数或方法。"""
    value: object = module
    for part in qualified_name.split("."):
        value = getattr(value, part)
    return value
