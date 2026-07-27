# Security Policy

## Supported versions

This project is pre-1.0. Security fixes are applied to the latest released
minor version only.

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a vulnerability

Please do not open a public issue for security reports.

Use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability), or email spencer@spencercmd.com.

Please include a description of the issue, the affected version, and steps to
reproduce. Expect an initial response within seven days.

## Scope notes

Two areas are worth calling out for anyone assessing this library.

**Telemetry data is caller-controlled.** Label values passed to
`observe_dependency` and `observe_llm`, and attributes attached to spans, are
forwarded as given. Do not pass secrets, credentials, tokens, or personal data
into them: those values end up in your metrics store and your tracing backend,
which usually have broader read access than your application logs.

**Outbound network calls.** Tracing is off unless
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, at which point spans are exported to that
endpoint. The optional ECS enricher reads the container metadata endpoint once
at startup with a 0.5 second timeout when `ECS_CONTAINER_METADATA_URI_V4` or
`ECS_CONTAINER_METADATA_URI` is present. No other network calls are made.

The `/metrics` endpoint is mounted by your application, not by this package;
restricting access to it is the deploying service's responsibility.
