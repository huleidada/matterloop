简体中文 | [English](releasing.en.md)

# 发布到公共 PyPI

MatterLoop 由 12 个独立发行包组成。仓库采用统一版本：一次 Release 中，所有包必须使用同一个
版本号，并由同一个 Git tag 触发发布。这样可以让内部依赖约束保持可预测，也避免用户安装到一组
彼此不兼容的组件。

> `v0.1.0` 已于 2026-07-16 发布到公共 PyPI。12 个发行包均包含 wheel 与 sdist，并通过
> [GitHub Actions 发布流程](https://github.com/huleidada/matterloop/actions/runs/29477478162)
> 完成公共索引安装验证。版本说明见 [GitHub Release](https://github.com/huleidada/matterloop/releases/tag/v0.1.0)。

## 发行范围

`v0.1.0` 包含以下 PyPI 项目：

| PyPI 项目 | Python 导入名 | 用途 |
| --- | --- | --- |
| `matterloop-core` | `matterloop_core` | 闭环内核与扩展协议 |
| `matterloop-models` | `matterloop_models` | 模型抽象、注册表与供应商适配器 |
| `matterloop-runtime` | `matterloop_runtime` | 异步、同步与队列运行时 |
| `matterloop-tools` | `matterloop_tools` | 工具注册、MCP、Skills 与内置工具 |
| `matterloop-memory` | `matterloop_memory` | 记忆与内存检查点实现 |
| `matterloop-policies` | `matterloop_policies` | 预算、重试、审批和权限策略 |
| `matterloop-agents` | `matterloop_agents` | 单 Agent 与 TeamLoop 协作能力 |
| `matterloop-observability` | `matterloop_observability` | 日志、Trace、Metrics 与脱敏 |
| `matterloop-presets` | `matterloop_presets` | 开箱即用的组件装配 |
| `matterloop-integration-fastapi` | `matterloop_integration_fastapi` | FastAPI 控制面适配 |
| `matterloop-integration-celery` | `matterloop_integration_celery` | Celery 队列适配 |
| `matterloop-integration-redis` | `matterloop_integration_redis` | Redis 队列、运行仓储和事件适配 |

所有发行包要求 Python 3.10 或更高版本。希望快速完成装配的用户安装
`matterloop-presets`；只需要底层协议或某项能力时，可以单独安装对应包。`presets` 会通过依赖关系
安装其所需的基础模块，框架集成包仍需按实际部署方式单独选择。

## 可信发布的一次性配置

发布使用 PyPI Trusted Publishing。GitHub Actions 通过 OIDC 获取一次性的、短时有效的上传凭据，
仓库不保存 `PYPI_API_TOKEN`，维护者也不需要在本地生成或复制长期 Token。

### 1. 配置 GitHub Environment

PyPI 不允许两个尚未创建的项目使用完全相同的 Pending Publisher 身份。首发阶段必须让每个包的
OIDC 声明可唯一匹配，因此 `publish.yml` 使用 12 个发布作业和下表所列 Environment。每个
Environment 都至少设置一位 Required reviewer；普通 CI 和未经确认的 tag 不能直接取得发布身份。

| PyPI 项目 | GitHub Environment |
| --- | --- |
| `matterloop-core` | `pypi` |
| `matterloop-models` | `pypi-models` |
| `matterloop-runtime` | `pypi-runtime` |
| `matterloop-tools` | `pypi-tools` |
| `matterloop-memory` | `pypi-memory` |
| `matterloop-policies` | `pypi-policies` |
| `matterloop-agents` | `pypi-agents` |
| `matterloop-observability` | `pypi-observability` |
| `matterloop-presets` | `pypi-presets` |
| `matterloop-integration-fastapi` | `pypi-integration-fastapi` |
| `matterloop-integration-celery` | `pypi-integration-celery` |
| `matterloop-integration-redis` | `pypi-integration-redis` |

Environment 中不添加 PyPI Token。每个发布作业只下载已经验证的统一制品，再筛选当前包的一个 wheel
和一个 sdist；只有该作业拥有 `id-token: write`。独立身份既满足首次项目创建约束，也让单包重试和
审计边界更清楚。

### 2. 首次发布时登记 Pending Publisher

登录 PyPI，在“Publishing”页面为每个项目分别登记一个 Pending Publisher。Owner、Repository 和
Workflow 相同，项目名与 Environment 使用上表中的对应值：

| 字段 | 值 |
| --- | --- |
| Owner | `huleidada` |
| Repository name | `matterloop` |
| Workflow name | `publish.yml` |
| Environment name | 上表中与项目对应的值 |

字段必须与 GitHub 上的实际名称逐字一致。Pending Publisher 不会提前占用项目名；首次 OIDC 上传
成功时，PyPI 才创建对应项目并将该 Publisher 转为正式配置。`v0.1.0` 发布后，12 个项目均已完成
这次转换；后续版本复用现有 Trusted Publisher，不再重复登记 Pending Publisher。

## 准备一个版本

发布提交应同时满足以下条件：

- 12 个 `pyproject.toml` 使用同一版本，内部依赖约束能够包含该版本；
- `CHANGELOG.md` 已把待发布内容归入确定版本，并写入发布日期；
- `main` 上的 Ruff、mypy、pytest、依赖方向检查以及 wheel/sdist 构建全部通过；
- 构建产物来自 Git tag 指向的提交，而不是维护者工作站中的未提交文件；
- tag 使用 `v<版本>` 格式，例如包版本 `0.1.0` 对应 `v0.1.0`。

本地可以在推送 tag 前执行与 CI 等价的检查：

```bash
uv sync --all-extras --dev --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/check_dependencies.py
uv build --all-packages
```

确认发布提交已经位于 `main` 后，创建带说明的 tag：

```bash
git tag -a v0.1.0 -m "release: v0.1.0"
git push github main v0.1.0
```

`publish.yml` 会以 tag 中的版本为发布边界。构建与产物检查成功后，12 个发布作业分别进入对应
Environment 等待人工批准。批准前应核对 tag、commit SHA、版本号和待上传的发行包；批准后每个
作业只为自己的 PyPI 项目换取短期凭据并上传两个制品。全部发布作业成功后才执行公共索引安装验证。

## 从 PyPI 验证发布

PyPI 的项目页和 Simple API 都能看到新版本后，再从一个干净环境验证，避免 uv workspace 或本地
wheel 掩盖依赖问题：

```bash
python -m venv /tmp/matterloop-pypi-check
/tmp/matterloop-pypi-check/bin/python -m pip install --upgrade pip
/tmp/matterloop-pypi-check/bin/python -m pip install \
  --index-url https://pypi.org/simple \
  --no-cache-dir \
  matterloop-presets==0.1.0
/tmp/matterloop-pypi-check/bin/python -m pip check
/tmp/matterloop-pypi-check/bin/python -c \
  "import matterloop_core, matterloop_models, matterloop_presets"
```

还应按需抽查独立组件和框架集成包，例如：

```bash
/tmp/matterloop-pypi-check/bin/python -m pip install \
  --index-url https://pypi.org/simple \
  matterloop-integration-fastapi==0.1.0
```

验证内容至少包括：安装来源是 `pypi.org`、`pip check` 没有依赖冲突、公开导入名可用，以及安装
过程中没有回退到本地路径或额外索引。

## 失败如何处理

发布不是跨 12 个项目的原子事务，故障处理以“PyPI 已经接受了哪些文件”为准。

- **构建或测试失败**：尚未上传时，修复后重新走完整检查。不要让本地构建产物绕过 GitHub
  Environment 直接上传。
- **OIDC 被拒绝**：核对 PyPI Publisher 的 owner、repository、workflow、environment 是否精确
  匹配上表，并确认任务声明了 `id-token: write` 且确实进入对应 Environment。无需创建 Token
  兜底。
- **网络或 PyPI 临时故障**：先检查各项目的 Release files，再重跑失败任务。已经存在且哈希正确的
  文件视为成功，不应重复构建一份内容不同的同名文件。
- **部分包已发布**：记录已成功的项目，只补发同版本中缺失的项目；全部到齐前不要宣布版本可用。
  同时验证 `matterloop-presets` 的依赖能从公共索引完整解析。
- **产物本身有问题**：发布修复版本，例如 `0.1.1`。PyPI 上的版本和文件不可覆盖；删除版本也不
  会使版本号变得可安全复用。
- **tag 或版本写错**：如果 PyPI 尚未接收任何文件，可以修正发布准备；一旦已有文件上传，就保留
  历史并递增版本。不要移动已经公开使用的 tag。

发布完成后，在 GitHub 创建与 tag 对应的 Release，并从 `CHANGELOG.md` 提取面向用户的变更。
GitHub Release 是发布记录，PyPI 才是 `pip install` 的公共制品来源。
