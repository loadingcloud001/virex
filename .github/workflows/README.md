# GitHub Workflows

CI/CD pipelines for the Virex AI edge stack.

> This directory intentionally ships empty for v1. CI is run locally
> via `uv run pytest` per module — see [`../README.md`](../README.md)
> "Quick Start (Developer)" for the full suite.
>
> GitHub Actions workflows are on the roadmap (see
> [`../ROADMAP.md`](../ROADMAP.md) banner).

## Planned Workflows

- `test.yml` — Run pytest + ruff + mypy across all four modules on every PR.
  Triggers: `pull_request`, `push` to `main`. Matrix: `ai-backend`,
  `edge-agent`, `event-router`, `portal`.
- `docker.yml` — Build the five container images
  (`virex-detector`, `virex-ai-backend`, `virex-event-router`,
  `virex-portal`, `virex-edge-agent`) on every push to `main`.
  Push to GitHub Container Registry (`ghcr.io/loadingcloud001/virex-*`).
- `deploy.yml` — Auto-deploy to the edge node (via `docker compose pull
  && docker compose up -d`) on tagged releases. **Manual approval
  gate** for production.

## Local Test Run (v1)

```bash
cd ai-backend && uv sync && uv run pytest     # 118 tests
cd ../edge-agent && uv sync && uv run pytest  #  29 tests
cd ../event-router && uv run pytest           #  12 tests
cd ../portal      && uv run pytest            #  Phase 2 API tests

# Lint
uv run ruff check .
uv run ruff format --check .
```