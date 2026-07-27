# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-27

Initial public release.

### Added

- HTTP RED metrics middleware (`http_requests_total`,
  `http_request_duration_seconds`) plus an `http_requests_in_flight`
  saturation gauge, with health/docs/metrics paths excluded.
- Low-cardinality route-template labels resolved from the FastAPI/Starlette
  route table, including routers and `Mount`s, with an LRU cache and opt-in
  negative caching via `setup_metrics(app, cache_unmatched=True)`.
- OpenMetrics scrape output from `metrics_response()` so histogram exemplars
  (`trace_id` / `span_id`) survive the scrape.
- `observe_dependency` and `observe_llm` context managers, each usable as both
  `with` and `async with`, emitting Prometheus histograms plus an
  OpenTelemetry child span.
- Opt-in OpenTelemetry tracing via `setup_tracing`, enabled only when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set, with fail-open construction.
- Optional client auto-instrumentation for `httpx`, `requests`, `sqlalchemy`,
  and `redis` through extras and `OTEL_INSTRUMENTATIONS`.
- Optional ECS resource enricher that adds `cloud.*` attributes only when ECS
  metadata is available, plus `register_resource_enricher` for custom sources.
- Configurable metric names through `METRICS_NAME_PREFIX` or
  `configure_metric_names`.

[Unreleased]: https://github.com/spencercmd/fastapi-vitals/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/spencercmd/fastapi-vitals/releases/tag/v0.1.0
