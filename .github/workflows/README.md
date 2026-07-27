# GitHub Workflows

CI/CD pipelines for the Frigate SaaS Platform.

## Planned Workflows

- `test.yml` - Run pytest + ruff + mypy on every PR (TBD - Week 2)
- `docker.yml` - Build Docker images on main branch (TBD - Week 3)
- `deploy.yml` - Auto-deploy to staging on main branch (TBD - Week 6)

## Setup

These workflows will be activated after Phase 1 MVP is validated.

For now, run tests locally:

```bash
cd portal
pytest
ruff check .
mypy .
```