# Contributing

Bug reports and focused pull requests are welcome. For broad behavior changes, an issue can help
settle the configuration contract before implementation.

## Setup

```console
git clone https://github.com/aspix2k/a2a-proof.git
cd a2a-proof
uv sync --all-groups
```

Before opening a pull request, run:

```console
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run python scripts/generate_schema.py --check
uv run zizmor --persona=pedantic --offline --strict-collection .
uv run pytest --cov=a2a_proof
uv build
```

Add tests for observable behavior. Keep changes small, avoid compatibility shims without a real
use case, and do not include credentials or responses from private agents.

The deterministic core also has mutation tests:

```console
uv run mutmut run --max-children 1
uv run mutmut results
```

## Releasing

Update the version in `pyproject.toml`, refresh `uv.lock`, and move the changelog entries out of
`Unreleased`. Use only the applicable `Features`, `Bug fixes`, `Security`, `Documentation`, and
`Maintenance` sections. Order sections and their entries from highest to lowest user impact; do
not use generated commit lists or vague summaries. After CI passes on `main`, create and push the
matching `vX.Y.Z` tag. The release workflow requires that version's changelog section, rebuilds
and tests the package, verifies the tag version, publishes through PyPI Trusted Publishing, records
build provenance, and publishes immutable GitHub release assets. Never move or reuse a release tag.
