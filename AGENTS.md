# AGENTS.md

Guidance for AI coding agents working in this repository.

`fastapi-vitals` is a telemetry library that runs inside other people's
production request paths. Two consequences shape every rule below: a bug here
degrades services that are not ours, and the metric series this package emits
are a public contract that dashboards and alerts are built on. Prefer the
smallest correct change. When in doubt, do less.

## Commands

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[test,dev]"

.venv/bin/ruff check src tests
.venv/bin/mypy
.venv/bin/pyright
.venv/bin/pytest -q
```

All four must pass; CI runs the same set on Python 3.10 through 3.13. `mypy`
and `pyright` are both required and disagree often enough that passing one is
not evidence for the other. `mypy` runs with `disallow_untyped_defs`, so every
new function needs annotations.

Benchmarks are excluded by default (`addopts = --benchmark-disable`). Run them
explicitly:

```bash
.venv/bin/pytest tests/test_bench_route_template.py --benchmark-only -q
```

## Layout

| Path | Role |
|------|------|
| `metrics/instruments.py` | Process-global Prometheus instruments, label tuples, bucket boundaries, bounded enums |
| `metrics/names.py` | Series-name resolution and the name lock |
| `metrics/middleware.py` | RED + in-flight middleware, `setup_metrics`, `metrics_response` |
| `metrics/route_templates.py` | Route-table walk and the bounded LRU that keeps `route` low-cardinality |
| `metrics/observe.py` | `observe_dependency` / `observe_llm` |
| `metrics/_dual_context.py` | Sync + async context-manager shell |
| `metrics/_exemplars.py` | Cached tracer and exemplar label extraction |
| `tracing.py` | `setup_tracing`, opt-in OTLP export |
| `_identity.py` | `SERVICE` / `ENV` / `APP_VERSION`, shared by metrics and trace resources |
| `_process_cache.py` | The freeze-on-first-use cache used by the above |
| `adapters/ecs.py` | Optional ECS resource enricher |

## Invariants

These are the things most likely to be broken by a well-intentioned change.
Several are already documented in module docstrings; read the docstring before
editing a module.

**Never widen label cardinality.** Every label value must come from a bounded
set: a route template, a caller-supplied peer name, or an enum this package
owns (`LLM_STATUS_VALUES`, `LLM_FINISH_REASONS`, `status_class`). Never let a
raw path, URL, user ID, exception message, SQL string, or prompt reach a label.
`route` is a template (`/items/{item_id}`) and falls back to the literal string
`unmatched`. Token counts are counter *values*, never labels. A cardinality
explosion takes down the user's Prometheus server, not just this library.

**Series names and label sets are a public API.** Renaming a metric, adding a
label, reordering a label tuple, or changing bucket boundaries silently breaks
existing dashboards, recording rules, and alerts. Treat any such change as
semver-breaking, and do not make one incidentally while fixing something else.

**Do not rename `observe_dependency` or `observe_llm` to CapWords.** They are
classes with function-style names on purpose, for call-site readability. Ruff's
naming rules are not enabled, so nothing will stop you; renaming them breaks
every downstream caller.

**Do not extract a generic observer.** `observe_dependency` and `observe_llm`
deliberately duplicate structure because their bodies specialize. `observe.py`
says to leave them alone until a third sibling exists. Two similar blocks is
not a reason to build an abstraction here.

**Fail open on the request path.** Route matching swallows exceptions from
custom routes and logs at debug. `setup_tracing` returns `False` rather than
raising when exporter construction fails. Instrumentor failures warn and
continue. Any new per-request code needs the same behavior plus a test proving
that a failure inside telemetry still lets the response through. Never let this
library be the reason a request 500s.

**Respect the process-global freeze.** `identity_labels()`, `metrics_enabled()`
and the metrics tracer are `ProcessCache` instances that read env once and
freeze. Metric names lock the first time `names.resolve` runs, which happens at
import of `instruments.py`. This means import order is load-bearing:
`configure_metric_names` only works before instruments are imported. Do not add
code that re-reads env per request.

**Snapshot labels once per request.** `metrics_middleware` resolves identity and
route before `call_next` and reuses the same tuple for the gauge increment and
decrement. If those two calls could ever see different label values, the gauge
leaks and never returns to zero.

**Keep the scrape in OpenMetrics.** `metrics_response` uses
`generate_latest_openmetrics` because classic Prometheus exposition silently
drops exemplars. Do not "simplify" it to `generate_latest`. Under
`PROMETHEUS_MULTIPROC_DIR`, scrape via a fresh `CollectorRegistry` +
`MultiProcessCollector` only — never fall back to `REGISTRY` when the env is
set. Keep `HTTP_REQUESTS_IN_FLIGHT` on `multiprocess_mode="livesum"` (not
`"all"`, which injects a `pid` label). The env must be present before importing
metrics (mmap `ValueClass` freezes at first construction).

**Duck typing in `route_templates.py` is deliberate.** The route table is
heterogeneous — FastAPI included routers, Starlette `Mount`s, and custom route
objects — so `effective_candidates` and `matches` are discovered with `getattr`
plus `callable`. Do not replace this with `isinstance` checks against concrete
classes.

**Do not add required dependencies.** New integrations belong in
`[project.optional-dependencies]` and must degrade with a warning when the
extra is absent, following the pattern in `_instrumentors.py`.

## Tests

The Prometheus `REGISTRY` and the instruments are process-global, so tests share
state. Two consequences:

- An autouse fixture in `conftest.py` resets the identity and metrics-enabled
  caches around every test. If you add a new `ProcessCache`, add a reset for it
  there too, or tests will pass or fail depending on ordering.
- Use the `memory_tracer` fixture rather than `set_tracer_provider`. OpenTelemetry
  only honors the global provider once per process, so claiming it in a test
  makes the suite order-dependent. The fixture patches `get_tracer` on
  `metrics._exemplars` instead.

Assert on collected samples via the helpers in `tests/metrics_helpers.py` where
possible, rather than string-matching scrape output; label ordering in the text
format is not something to couple tests to.

Cover both `with` and `async with` for anything touching `DualContext`, and
cover the exception path, since status labels and span status are set in
`finally` and `except` blocks.

## Style

Match the surrounding code rather than modernizing it. Modules use
`from __future__ import annotations` with `Optional` / `Tuple` style
annotations; leave that alone even though the 3.10 floor permits `X | None`.
Line length is 100. Comments in this codebase explain why a constraint exists,
usually a Prometheus or OpenTelemetry behavior that is not obvious from the
code; do not add comments that restate what the next line does.

## Before you finish

- Run all four checks.
- Add a `## [Unreleased]` entry to `CHANGELOG.md` for user-visible changes.
- Update `README.md` for any public API change; it is the reference
  documentation for this project.
- Call out any new metric series or label explicitly in your summary so a human
  can review the cardinality decision.
- Do not bump `__version__` or tag releases unless asked. Version lives only in
  `src/fastapi_vitals/__init__.py`; `pyproject.toml` reads it from there.
