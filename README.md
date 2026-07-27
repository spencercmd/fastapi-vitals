# fastapi-observability

OpenTelemetry tracing and Prometheus **RED** metrics for FastAPI services:
rate, errors, duration, plus an in-flight **saturation** gauge
(`http_requests_in_flight`).

Designed for production request paths: low-cardinality **route templates**,
**OpenMetrics** scrapes with histogram **exemplars**, fail-open middleware, and
dual sync/async dependency/LLM observers.

## Install

```bash
pip install fastapi-observability

# Optional client auto-instrumentation extras:
pip install "fastapi-observability[httpx]"
pip install "fastapi-observability[otel-instrumentations]"  # httpx, requests, sqlalchemy, redis
```

From a local checkout:

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[test,dev]"
```

## Quick start

```python
from fastapi import FastAPI
from fastapi_observability.metrics import setup_metrics, metrics_response
from fastapi_observability.tracing import setup_tracing, shutdown_tracing

app = FastAPI()
setup_tracing(app)   # no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set
# Optional client auto-instrumentation (requires matching extras):
# setup_tracing(app, instrument=["httpx", "requests"])
setup_metrics(app)

@app.get("/metrics")
def metrics():
    return metrics_response()
```

Set identity env vars in every deployment:

```bash
export SERVICE=my-api
export ENV=prod
export APP_VERSION=1.2.3
```

## What you get

| Capability | Notes |
|------------|--------|
| HTTP RED + in-flight gauge | Middleware; excluded health/docs/metrics paths |
| Route-template labels | Walks FastAPI/Starlette routes (routers, Mounts); LRU cache |
| OpenMetrics scrape | Exemplars (`trace_id` / `span_id`) retained |
| `observe_dependency` | Dual `with` / `async with`; histogram + OTEL child span |
| `observe_llm` | Tokens, finish_reason, rate_limited status; dual context |
| Optional OTEL clients | `httpx`, `requests`, `sqlalchemy`, `redis` via extras |
| Optional ECS enricher | Cloud resource attrs only when ECS metadata URI is set |

## OpenMetrics scrape format

`metrics_response()` returns **OpenMetrics** text
(`Content-Type: application/openmetrics-text; version=1.0.0; charset=utf-8`),
not classic Prometheus exposition. This is required so histogram **exemplars**
survive the scrape.

- Prometheus 2.x / Grafana Agent / Alloy and most modern scrapers accept OpenMetrics.
- Body ends with `# EOF` (OpenMetrics framing).

## Multiproc support matrix

| Deployment | `http_requests_*` counters/histograms | `http_requests_in_flight` Gauge | Notes |
|------------|---------------------------------------|----------------------------------|--------|
| Single process (Uvicorn/Gunicorn **1 worker**) | ✅ | ✅ | Supported default |
| Multi-worker **without** `PROMETHEUS_MULTIPROC_DIR` | ⚠️ per-process scrapes only | ⚠️ per-process only | Prefer one scrape target per worker or stick to 1 worker |
| Multi-worker **with** `PROMETHEUS_MULTIPROC_DIR` | ❌ not wired | ❌ not wired | Needs `multiprocess_mode="livesum"` + `MultiProcessCollector` — not implemented |

Env must be set **before** importing metrics once multiproc lands.

## Prometheus label cardinality rules

Keep high-cardinality values **off** label sets. Route templates are already
low-cardinality (`/items/{item_id}`, never raw IDs). For dependency and LLM
series, callers control the free-form labels:

| Label | Series | Rule |
|-------|--------|------|
| `dependency` | `dependency_request_duration_seconds` | Stable peer name (`openai`, `sql`, `redis`) — **not** hostnames, request IDs, or SQL text |
| `operation` | dependency + LLM histograms/counters | Coarse verb or use-case (`chat`, `query`, `embed`) — **not** per-user or per-document IDs |
| `provider` | LLM series | Fixed vendor/runtime (`openai`, `transformers`) |
| `model` | LLM series | Model id as deployed (`gpt-4o-mini`, `my-classifier-v1`) — avoid free-form user input; prefer a small allowlist per service |
| `status` / `finish_reason` / `token_type` / `status_class` | various | Package-bounded enums only |
| `route` | HTTP RED / in-flight | Template from route table (`unmatched` when none); never raw paths with IDs |

**Anti-patterns:** putting customer IDs, full URLs, exception messages, or
prompt text into any label. Cardinality explosions break Prometheus memory and
cross-series joins. Token **counts** are counter *values* on `llm_tokens_total`,
not labels.

## Dependency spans: two paths (pick one per library)

| Path | Owner | Use when |
|------|--------|----------|
| `observe_dependency(...)` | **metrics** (histogram primary; OTEL child span secondary) | Explicit `dependency` / `operation` labels; clients without an instrumentor (e.g. OpenAI SDK); critical SQL/Redis |
| `setup_tracing(..., instrument=[...])` | **tracing** (library auto-spans only) | Blanket coverage without wrapping every call |

**Do not double-wrap.** Enabling both for the same outbound call produces two
child spans in APM. Prefer instrumentors for `httpx` / `requests`; prefer
`observe_dependency` when you need RED metrics on the call.

### Optional client instrumentations

```bash
pip install "fastapi-observability[httpx]"
pip install "fastapi-observability[otel-instrumentations]"
```

```python
setup_tracing(app, instrument=["httpx"])
# or: OTEL_INSTRUMENTATIONS=httpx,requests
```

Supported names: `httpx`, `requests`, `sqlalchemy`, `redis`.

- **`httpx` / `requests`**: process-global client instrumentation works well for
  typical usage.
- **`sqlalchemy` / `redis`**: best-effort only. This package calls bare
  `.instrument()` with no engine/client wiring. Prefer `observe_dependency`
  for critical SQL/Redis paths until engine-scoped wiring is supported.

Missing optional extras are skipped with a warning; tracing still enables.

### Manual dependency timing

```python
from fastapi_observability.metrics import observe_dependency

with observe_dependency("openai", "chat"):
    ...  # dependency_request_duration_seconds + span "dependency openai"

async with observe_dependency("sql", "query"):
    ...
```

Span attributes: `peer.service`, plus `dependency` / `operation` aligned with
the histogram labels. Exceptions set span status ERROR and `record_exception`;
histogram `status` is `ok` or `error`.

### LLM calls (additive)

Prefer `observe_llm` for OpenAI-class and local transformers so you get model,
finish reason, and token counters — **instead of** nesting `observe_dependency`
around the same call (that would double-count duration).

```python
from fastapi_observability.metrics import observe_llm

with observe_llm("openai", "gpt-4o-mini", "chat") as obs:
    response = client.chat.completions.create(...)
    usage = response.usage
    obs.set_result(
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        finish_reason=response.choices[0].finish_reason or "unknown",
    )

# Distinguish rate limits for alerting (call before re-raise)
with observe_llm("openai", model, "chat") as obs:
    try:
        ...
    except RateLimitError:
        obs.set_status("rate_limited")
        raise
```

| Metric | Labels (beyond service/env/version) |
|--------|-------------------------------------|
| `llm_request_duration_seconds` | `provider`, `model`, `operation`, `status`, `finish_reason` |
| `llm_tokens_total` | `provider`, `model`, `operation`, `token_type` (`input`\|`output`) |

`status` is `ok`, `error`, or `rate_limited` (unknown strings coerce to
`error`). Non-ok status marks the OTEL span ERROR even without an exception.
`finish_reason` is cardinality-bounded: `stop`, `length`, `content_filter`,
`tool_calls`, `unknown`, or free-form → `other`.

Structured-log correlation:

```python
from fastapi_observability.tracing import get_trace_context_ids
trace_id, span_id = get_trace_context_ids()   # ("-", "-") when no active span
```

Custom metrics with the same service/env/version identity as RED series:

```python
from fastapi_observability.metrics import identity_labels

# (service, env, version) from SERVICE / ENV / APP_VERSION
MY_COUNTER.labels(*identity_labels(), "some_dim").inc()
```

## Configurable metric names

Default series names are the standard RED-style names above. To namespace them
(e.g. multi-app processes), set a prefix **before** importing instruments:

```bash
export METRICS_NAME_PREFIX=myapp   # → myapp_http_requests_total, …
```

Or in bootstrap code **before** `from fastapi_observability.metrics import …`:

```python
from fastapi_observability.metrics.names import configure_metric_names

configure_metric_names(prefix="myapp")
# or full overrides:
# configure_metric_names(names={"http_requests": "svc_http_requests_total"})

from fastapi_observability.metrics import setup_metrics, HTTP_REQUESTS
```

Names lock on first instrument construction; mid-process renames are not supported.

## Configuration (env)

| Var | Purpose | Default |
|-----|---------|---------|
| `SERVICE` | RED `service` label + tracer service name | `app` |
| `ENV` | RED `env` label / deployment environment | `prod` |
| `APP_VERSION` | RED `version` label / service version | `unknown` |
| `METRICS_ENABLED` | Toggle metrics collection + `/metrics` | `true` |
| `METRICS_NAME_PREFIX` | Prefix all default metric series names | unset (no prefix) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Enables tracing when set | unset (tracing off) |
| `OTEL_SERVICE_NAME` | Overrides tracer service name | falls back to `SERVICE` |
| `OTEL_INSTRUMENTATIONS` | Comma-separated client instrumentors | unset (none) |
| `OTEL_TRACES_SAMPLER` | Sampler name (SDK) | `parentbased_always_on` |
| `OTEL_TRACES_SAMPLER_ARG` | Sampler arg (ratio for `*traceidratio`) | `1.0` when ratio sampler and unset |
| `OTEL_BSP_*` / `OTEL_EXPORTER_OTLP_*` | Batch export / OTLP HTTP knobs | SDK defaults |
| `ECS_CONTAINER_METADATA_URI_V4` / `ECS_CONTAINER_METADATA_URI` | Enables optional ECS resource enricher | unset (no cloud.* attrs) |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | Preferred `cloud.region` on ECS | derived from AZ when missing |
| `HOSTNAME` | Preferred `service.instance.id` off-ECS | `socket.gethostname()` |

Always set `SERVICE` in real deployments; the `app` default is for local/dev only.

### Tracing sampling & export

Tracing stays **opt-in**: unset `OTEL_EXPORTER_OTLP_ENDPOINT` and
`setup_tracing` is a no-op. When enabled, this package builds
`TracerProvider` + `BatchSpanProcessor(OTLPSpanExporter())` with default
constructor kwargs so the **OpenTelemetry Python SDK** resolves sampling and
batch/export knobs from the standard env vars.

Recommended for production (respects inbound parent decisions, caps volume):

```bash
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.1
```

**Fail-open.** If exporter/processor construction fails, `setup_tracing` logs
and returns `False` so the service still starts. Individual client instrumentor
failures after the provider is committed are skipped with a warning; tracing
stays enabled. Request-path route matching failures log at debug and fall back
to `unmatched` rather than breaking the request.

### Resource attributes (optional ECS adapter)

Always set on the tracer resource:

- `service.name`, `service.version`, `deployment.environment`
- `process.runtime.name` / `.version` / `.description`
- `service.instance.id` when known (`HOSTNAME` → hostname)

**ECS is optional.** When an ECS metadata URI env var is present, the
`fastapi_observability.adapters.ecs` enricher also sets:

- `cloud.provider=aws`, `cloud.platform=aws_ecs`
- `cloud.region` from `AWS_REGION` / `AWS_DEFAULT_REGION` or the task AZ
- prefers the ECS task id for `service.instance.id`

Setup works with zero cloud metadata. Pass `enrichers=()` to
`build_resource_attributes` to disable auto-detection, or register custom
enrichers via `register_resource_enricher`.

**Never writes** `cloud.account.id` — task id is only the TaskARN last path
segment. Operator-supplied `OTEL_RESOURCE_ATTRIBUTES` is merged by the OTEL
SDK and is out of scope for this package.

Metadata is fetched **once** at `setup_tracing` with a **0.5s** timeout and
fails open.

## Route-template cache

By default, `unmatched` is **not** memoized so late route registration cannot
stick permanent misses. After routes are stable (typical production boot), you
may enable negative caching:

```python
setup_metrics(app, cache_unmatched=True)
```

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[test,dev]"
.venv/bin/ruff check src tests
.venv/bin/mypy
.venv/bin/pyright
.venv/bin/pytest -q
```

### Route-template micro-bench (optional)

```bash
.venv/bin/python scripts/bench_route_template.py
.venv/bin/pytest tests/test_bench_route_template.py --benchmark-only -q
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
