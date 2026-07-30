# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-30

### Added

- Multi-worker Prometheus support via `PROMETHEUS_MULTIPROC_DIR`:
  `http_requests_in_flight` uses `multiprocess_mode="livesum"`,
  `metrics_response()` scrapes through `MultiProcessCollector` when the env
  key is present and the path is a directory (raw env string, matching
  prometheus_client writers), and `mark_process_dead` is exported for
  Gunicorn `worker_exit` bookkeeping (`mark_process_dead` no-ops when the
  env key is absent, blank, or not a directory). Empty/invalid multiproc
  dirs and collector errors fail soft to empty OpenMetrics (never fall
  back to in-process `REGISTRY` while the env key is set). An empty
  `PROMETHEUS_MULTIPROC_DIR=` assignment is not "off"; unset the variable
  to disable. Exemplars remain single-process-only (upstream mmap
  limitation).
- Grafana RED overview dashboard JSON under `dashboards/`, plus a production
  screenshot in `docs/images/`, so adopters can import panels keyed to the
  public `http_*` / `dependency_*` series without reverse-engineering PromQL.

### Breaking (spans only)

- `observe_llm` child spans now follow OpenTelemetry GenAI inference
  conventions: name `{operation} {model}`, kind `CLIENT`, attributes
  `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.operation.name`
  (passed at span creation for attribute-based samplers),
  `gen_ai.response.finish_reasons`, and `error.type` on failures:
  `rate_limited` when that Prom status was set, else the OpenTelemetry
  exception type string (`module.QualName`, builtins unqualified) when
  recorded, else the non-ok Prom status (replacing custom `provider` /
  `model` / `operation` / `status` / `finish_reason` attrs and the
  `llm {provider}` span name). Exception paths keep the status description
  as `str(exc)`. `BaseException` (including `asyncio.CancelledError`) is
  recorded and re-raised. Instance state (status, finish_reason, tokens,
  exception) resets on each enter; call `set_status` / `set_result` inside
  the block. Prometheus `llm_*` series and labels are unchanged. Anyone
  querying the old custom span attrs or names must update their APM
  queries.

### Changed

- `observe_dependency` now catches `BaseException` (including
  `asyncio.CancelledError`) the same way as `observe_llm`: Prom
  `status=error`, span ERROR + `record_exception`, then re-raise. Previously
  cancellation left dependency histogram `status=ok` and skipped span ERROR.
- `observe_dependency` disables OpenTelemetry SDK auto exception/status on its
  child span so APM no longer gets a duplicate exception event alongside the
  library's explicit `record_exception`.
- `observe_dependency` and `observe_llm` treat `GeneratorExit` as abnormal
  context-manager close (not a call outcome): re-raise without error
  marking / `record_exception`, and without recording the duration
  histogram. Exit telemetry is fail-open so a metrics or span-annotation
  failure cannot suppress an in-flight exception or fail the call.

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

[Unreleased]: https://github.com/spencercmd/fastapi-vitals/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/spencercmd/fastapi-vitals/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/spencercmd/fastapi-vitals/releases/tag/v0.1.0
