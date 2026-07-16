[简体中文](releasing.md) | English

# Publishing to the public PyPI index

MatterLoop consists of 12 independent distributions. The repository uses synchronized versions: every package in one
Release must have the same version and must be published from the same Git tag. This keeps internal dependency
constraints predictable and prevents users from installing an incompatible combination of components.

> `v0.1.0` was published to the public PyPI index on July 16, 2026. All 12 distributions include both a wheel and an
> sdist, and public-index installation was verified by the
> [GitHub Actions publishing run](https://github.com/huleidada/matterloop/actions/runs/29477478162).
> See the [GitHub Release](https://github.com/huleidada/matterloop/releases/tag/v0.1.0) for release notes.

## Release scope

`v0.1.0` includes the following PyPI projects:

| PyPI project | Python import name | Purpose |
| --- | --- | --- |
| `matterloop-core` | `matterloop_core` | Loop kernel and extension protocols |
| `matterloop-models` | `matterloop_models` | Model abstractions, registry, and provider adapters |
| `matterloop-runtime` | `matterloop_runtime` | Asynchronous, synchronous, and queue runtimes |
| `matterloop-tools` | `matterloop_tools` | Tool registration, MCP, Skills, and built-in tools |
| `matterloop-memory` | `matterloop_memory` | Memory and in-memory checkpoint implementation |
| `matterloop-policies` | `matterloop_policies` | Budget, retry, approval, and permission policies |
| `matterloop-agents` | `matterloop_agents` | Single-Agent and TeamLoop collaboration capabilities |
| `matterloop-observability` | `matterloop_observability` | Logging, Trace, Metrics, and redaction |
| `matterloop-presets` | `matterloop_presets` | Ready-to-use component assembly |
| `matterloop-integration-fastapi` | `matterloop_integration_fastapi` | FastAPI control-plane adapter |
| `matterloop-integration-celery` | `matterloop_integration_celery` | Celery queue adapter |
| `matterloop-integration-redis` | `matterloop_integration_redis` | Redis queue, run repository, and event adapters |

All distributions require Python 3.10 or later. Users who want the fastest assembly path install
`matterloop-presets`; users who need only a lower-level protocol or one capability may install that distribution on
its own. `presets` installs its required foundation modules through dependencies, while framework integrations must
still be selected separately for the actual deployment.

## One-time trusted-publishing configuration

Releases use PyPI Trusted Publishing. GitHub Actions obtains a one-time, short-lived upload credential through OIDC.
The repository does not store `PYPI_API_TOKEN`, and maintainers do not need to generate or copy a long-lived Token on
their workstation.

### 1. Configure GitHub Environments

PyPI does not allow two projects that do not yet exist to use an identical Pending Publisher identity. During the
initial release, each package therefore needed an OIDC claim that could be matched uniquely. `publish.yml` uses 12
publishing jobs and the Environments listed below. Every Environment has at least one Required reviewer, so ordinary
CI and an unapproved tag cannot directly obtain a publishing identity.

| PyPI project | GitHub Environment |
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

Do not add a PyPI Token to an Environment. Each publishing job downloads the unified, verified artifact bundle and
then selects one wheel and one sdist for its current package. Only that job receives `id-token: write`. The independent
identities both satisfy the initial project-creation constraint and provide clearer single-package retry and audit
boundaries.

### 2. Register Pending Publishers for the first release

Sign in to PyPI and register one Pending Publisher for each project on the “Publishing” page. Owner, Repository, and
Workflow are identical; project name and Environment use their corresponding values from the table above:

| Field | Value |
| --- | --- |
| Owner | `huleidada` |
| Repository name | `matterloop` |
| Workflow name | `publish.yml` |
| Environment name | The value corresponding to the project in the table above |

Every field must match the actual GitHub name exactly. A Pending Publisher does not reserve a project name in
advance. PyPI creates the project and converts the Publisher to a normal configuration only after the first OIDC
upload succeeds. All 12 projects completed this conversion when `v0.1.0` was released. Later versions reuse the
existing Trusted Publishers and do not register Pending Publishers again.

## Preparing a release

The release commit must satisfy all of these conditions:

- all 12 `pyproject.toml` files use the same version, and internal dependency constraints contain that version;
- [CHANGELOG](../CHANGELOG.en.md) assigns pending changes to a concrete version and includes the release date;
- Ruff, mypy, pytest, the dependency-direction check, and wheel/sdist builds all pass on `main`;
- artifacts are built from the commit referenced by the Git tag, not from uncommitted files on a maintainer's
  workstation;
- the tag uses `v<version>` format; for example, package version `0.1.0` corresponds to `v0.1.0`.

Before pushing a tag, run the CI-equivalent checks locally:

```bash
uv sync --all-extras --dev --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/check_dependencies.py
uv build --all-packages
```

After confirming that the release commit is on `main`, create an annotated tag:

```bash
git tag -a v0.1.0 -m "release: v0.1.0"
git push github main v0.1.0
```

`publish.yml` treats the version in the tag as the release boundary. After build and artifact checks pass, the 12
publishing jobs enter their respective Environments and wait for human approval. Before approval, verify the tag,
commit SHA, version, and distribution to upload. After approval, each job exchanges its own identity for a short-lived
credential scoped to its PyPI project and uploads two artifacts. Public-index installation verification runs only
after every publishing job succeeds.

## Verifying a release from PyPI

After the version is visible on both the PyPI project pages and Simple API, verify it in a clean environment so the uv
workspace or local wheels cannot conceal a dependency problem:

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

Also spot-check independent components and framework integrations as appropriate. For example:

```bash
/tmp/matterloop-pypi-check/bin/python -m pip install \
  --index-url https://pypi.org/simple \
  matterloop-integration-fastapi==0.1.0
```

At a minimum, verify that installation came from `pypi.org`, `pip check` reports no dependency conflict, public import
names work, and installation did not fall back to a local path or additional index.

## Handling failures

Publishing is not atomic across all 12 projects. Failure handling must start from the files PyPI has already accepted.

- **Build or test failure:** if nothing has been uploaded, fix the issue and repeat the full set of checks. Do not
  bypass the GitHub Environment by uploading a local build directly.
- **OIDC rejection:** confirm that the PyPI Publisher's owner, repository, workflow, and environment exactly match the
  table above. Confirm that the job declares `id-token: write` and actually entered the corresponding Environment. Do
  not create a Token as a fallback.
- **Temporary network or PyPI failure:** inspect Release files for every project before rerunning the failed job. A
  file that already exists with the correct hash is successful; do not rebuild different content under the same file
  name.
- **Only some packages were published:** record the successful projects and publish only the missing projects at the
  same version. Do not announce the release until all are present. Also verify that `matterloop-presets` dependencies
  resolve completely from the public index.
- **The artifact itself is defective:** publish a corrective version such as `0.1.1`. PyPI versions and files cannot
  be overwritten; deleting a version does not make its version number safe to reuse.
- **The tag or version is wrong:** if PyPI has not accepted any file, correct the release preparation. Once any file
  has been uploaded, retain the history and increment the version. Do not move a tag that is already in public use.

After publishing, create a GitHub Release for the tag and extract user-facing changes from
[CHANGELOG](../CHANGELOG.en.md). A GitHub Release is the release record; PyPI is the public artifact source used by
`pip install`.
