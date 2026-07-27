# Contributing to fastapi-vitals

Thanks for your interest in improving this project. This library sits on the
request path of production services, so the bar for changes is "safe by
default": fail open, keep label cardinality bounded, and never break a request
because telemetry misbehaved.

## Development setup

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[test,dev]"
```

## Checks

All four must pass before a pull request can merge; CI runs the same commands.

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy
.venv/bin/pyright
.venv/bin/pytest -q
```

The optional route-template benchmark is excluded from the default run:

```bash
.venv/bin/pytest tests/test_bench_route_template.py --benchmark-only -q
```

## Project-specific rules

These are the constraints most likely to trip up an otherwise good patch.

**Never add an unbounded metric label.** Every label value must come from a
small, package-bounded set: a route template, a caller-supplied peer name, or
an enum this package controls. Customer IDs, raw URLs, exception messages, SQL
text, and prompt content must not become label values. A cardinality explosion
takes down the Prometheus server, not just this library. See the label rules
table in the README for the per-series contract.

**Stay fail-open on the request path.** Setup helpers, route matching, and
exporter construction all log and continue rather than raising. If you add a
code path that runs per request, it needs the same treatment plus a test that
proves a failure there still lets the response through.

**Do not double-instrument.** `observe_dependency` and the OpenTelemetry client
instrumentors both create child spans; the README documents which to use when.
New helpers should not add a third overlapping path for the same call.

**Metric names lock on first instrument construction.** Anything touching
`metrics/names.py` must preserve that, since renaming series mid-process would
silently split time series in existing dashboards.

## Pull requests

- Keep changes focused, and include tests for new behavior.
- Add a `## [Unreleased]` entry to `CHANGELOG.md` for anything user-visible.
- New public API needs a README section; this project treats the README as the
  reference documentation.
- Note any new metric series or label in the PR description so cardinality can
  be reviewed explicitly.

## Releasing

Maintainers only. Bump `__version__` in `src/fastapi_vitals/__init__.py` (the
single source of truth; `pyproject.toml` reads it), move the `Unreleased`
changelog entries under the new version, then tag:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

The `release` workflow builds the distributions and publishes to PyPI through
trusted publishing.

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0, consistent with the rest of the project.
