"""Multiprocess Prometheus scrape tests.

``prometheus_client`` freezes ``ValueClass`` at first metric construction, so
true multiproc enablement cannot be monkeypatched after ``instruments`` import
in the pytest process. Coverage runs in subprocesses with
``PROMETHEUS_MULTIPROC_DIR`` set before import.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Optional

import pytest

# Identity env for child writers so label sets are stable and joinable.
_CHILD_IDENTITY = {
    "SERVICE": "multiproc-test",
    "ENV": "test",
    "APP_VERSION": "0",
}


def _run_child(
    code: str,
    *,
    multiproc_dir: Optional[str] = None,
    extra_env: Optional[Mapping[str, str]] = None,
    drop_env: Optional[FrozenSet[str]] = None,
) -> subprocess.CompletedProcess[str]:
    env: Dict[str, str] = {**os.environ, **_CHILD_IDENTITY}
    if drop_env:
        for key in drop_env:
            env.pop(key, None)
    if multiproc_dir is not None:
        env["PROMETHEUS_MULTIPROC_DIR"] = multiproc_dir
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"child failed ({result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _sample_line(body: str, prefix: str, *, route: str = "/ping") -> str:
    for line in body.splitlines():
        if line.startswith(prefix) and f'route="{route}"' in line:
            return line
    pytest.fail(f"expected {prefix} sample with route={route!r}; scrape was:\n{body}")


def test_multiproc_scrape_aggregates_and_omits_pid_label(tmp_path: Path) -> None:
    """Child writes RED + in-flight; scrape via MultiProcessCollector has no pid."""
    multiproc_dir = str(tmp_path)
    write = _run_child(
        """
        from fastapi_vitals.metrics import (
            HTTP_REQUESTS,
            HTTP_REQUEST_DURATION,
            HTTP_REQUESTS_IN_FLIGHT,
            identity_labels,
            metrics_response,
        )

        svc, env, ver = identity_labels()
        HTTP_REQUESTS.labels(svc, env, ver, "GET", "/ping", "2xx").inc()
        HTTP_REQUEST_DURATION.labels(svc, env, ver, "GET", "/ping", "2xx").observe(0.01)
        HTTP_REQUESTS_IN_FLIGHT.labels(svc, env, "GET", "/ping").inc()
        body = metrics_response().body.decode()
        print(body)
        """,
        multiproc_dir=multiproc_dir,
    )
    _assert_ok(write)
    body = write.stdout
    assert _sample_line(body, "http_requests_total{").endswith(" 1.0")
    assert "http_requests_in_flight{" in body
    # livesum must not inject pid (default "all" would).
    for line in body.splitlines():
        if line.startswith("http_requests_in_flight{"):
            assert "pid=" not in line
            assert line.endswith(" 1.0")
            break
    else:
        pytest.fail("expected http_requests_in_flight sample")
    assert "# EOF" in body


def test_multiproc_two_workers_sum_counter_and_histogram(tmp_path: Path) -> None:
    """Two sequential child writers aggregate counter + histogram on scrape."""
    multiproc_dir = str(tmp_path)
    writer = """
        from fastapi_vitals.metrics import (
            HTTP_REQUESTS,
            HTTP_REQUEST_DURATION,
            identity_labels,
        )

        svc, env, ver = identity_labels()
        HTTP_REQUESTS.labels(svc, env, ver, "GET", "/ping", "2xx").inc()
        HTTP_REQUEST_DURATION.labels(svc, env, ver, "GET", "/ping", "2xx").observe(0.01)
        """
    _assert_ok(_run_child(writer, multiproc_dir=multiproc_dir))
    _assert_ok(_run_child(writer, multiproc_dir=multiproc_dir))

    scrape = _run_child(
        """
        from fastapi_vitals.metrics import metrics_response

        print(metrics_response().body.decode())
        """,
        multiproc_dir=multiproc_dir,
    )
    _assert_ok(scrape)
    body = scrape.stdout
    assert _sample_line(body, "http_requests_total{").endswith(" 2.0")
    assert _sample_line(body, "http_request_duration_seconds_count{").endswith(" 2.0")
    sum_line = _sample_line(body, "http_request_duration_seconds_sum{")
    assert float(sum_line.rsplit(" ", 1)[-1]) == pytest.approx(0.02)
    bucket_lines = [
        line
        for line in body.splitlines()
        if line.startswith("http_request_duration_seconds_bucket{")
        and 'route="/ping"' in line
    ]
    assert bucket_lines, f"expected histogram buckets; scrape was:\n{body}"
    inf = next((line for line in bucket_lines if 'le="+Inf"' in line), None)
    assert inf is not None and inf.endswith(" 2.0")


def test_multiproc_invalid_dir_fails_soft(tmp_path: Path) -> None:
    """Env set but path not a directory → empty OpenMetrics 200, no exception."""
    # Import needs a real dir (mmap create); scrape re-reads env and fail-softs.
    multiproc_dir = str(tmp_path)
    missing = str(tmp_path / "does-not-exist")
    result = _run_child(
        f"""
        from fastapi_vitals.metrics import metrics_response
        import os

        os.environ["PROMETHEUS_MULTIPROC_DIR"] = {missing!r}
        resp = metrics_response()
        print(resp.status_code)
        print(resp.media_type)
        print(resp.body.decode())
        """,
        multiproc_dir=multiproc_dir,
    )
    _assert_ok(result)
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "200"
    assert "openmetrics-text" in lines[1]
    assert lines[2] == "# EOF"
    assert "is not a directory" in result.stderr


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_multiproc_blank_env_does_not_use_registry(
    tmp_path: Path, blank: str
) -> None:
    """Key present but empty/whitespace → empty EOF, never REGISTRY scrape."""
    multiproc_dir = str(tmp_path)
    result = _run_child(
        f"""
        from fastapi_vitals.metrics import (
            HTTP_REQUESTS,
            identity_labels,
            metrics_response,
        )
        import os

        svc, env, ver = identity_labels()
        HTTP_REQUESTS.labels(svc, env, ver, "GET", "/ping", "2xx").inc()
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = {blank!r}
        resp = metrics_response()
        print(resp.status_code)
        print(resp.media_type)
        print(resp.body.decode())
        """,
        multiproc_dir=multiproc_dir,
    )
    _assert_ok(result)
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "200"
    assert "openmetrics-text" in lines[1]
    body = "\n".join(lines[2:])
    assert body.strip() == "# EOF"
    assert "http_requests_total" not in body
    assert "python_gc" not in body
    assert "is not a directory" in result.stderr


def test_multiproc_lowercase_env_scrape(tmp_path: Path) -> None:
    """Deprecated lowercase-only env still enables multiproc scrape path."""
    multiproc_dir = str(tmp_path)
    result = _run_child(
        """
        from fastapi_vitals.metrics import (
            HTTP_REQUESTS,
            identity_labels,
            metrics_response,
        )

        svc, env, ver = identity_labels()
        HTTP_REQUESTS.labels(svc, env, ver, "GET", "/ping", "2xx").inc()
        body = metrics_response().body.decode()
        print(body)
        """,
        multiproc_dir=None,
        drop_env=frozenset({"PROMETHEUS_MULTIPROC_DIR", "prometheus_multiproc_dir"}),
        extra_env={"prometheus_multiproc_dir": multiproc_dir},
    )
    _assert_ok(result)
    body = result.stdout
    assert _sample_line(body, "http_requests_total{").endswith(" 1.0")
    assert "# EOF" in body


def test_multiproc_corrupt_db_fails_soft(tmp_path: Path) -> None:
    """Unreadable/corrupt mmap file → empty OpenMetrics 200, no exception."""
    multiproc_dir = str(tmp_path)
    # Seed a real writer file, then overwrite with garbage the collector cannot merge.
    _assert_ok(
        _run_child(
            """
            from fastapi_vitals.metrics import HTTP_REQUESTS, identity_labels

            svc, env, ver = identity_labels()
            HTTP_REQUESTS.labels(svc, env, ver, "GET", "/ping", "2xx").inc()
            """,
            multiproc_dir=multiproc_dir,
        )
    )
    for db in tmp_path.glob("*.db"):
        db.write_bytes(b"not-a-valid-mmap-file\x00\xff")

    result = _run_child(
        """
        from fastapi_vitals.metrics import metrics_response

        resp = metrics_response()
        print(resp.status_code)
        print(resp.media_type)
        print(resp.body.decode())
        """,
        multiproc_dir=multiproc_dir,
    )
    _assert_ok(result)
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "200"
    assert "openmetrics-text" in lines[1]
    assert lines[2] == "# EOF"
    assert "multiproc metrics scrape failed" in result.stderr


def test_multiproc_metrics_disabled_still_404(tmp_path: Path) -> None:
    """METRICS_ENABLED=false returns 404 even when multiproc env is set."""
    result = _run_child(
        """
        from fastapi_vitals.metrics import metrics_response

        resp = metrics_response()
        print(resp.status_code)
        print(resp.body.decode() if resp.body else "")
        """,
        multiproc_dir=str(tmp_path),
        extra_env={"METRICS_ENABLED": "false"},
    )
    _assert_ok(result)
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "404"
    assert "http_requests" not in result.stdout


def test_mark_process_dead_removes_livesum_gauge_file(tmp_path: Path) -> None:
    """mark_process_dead clears a dead worker's live gauge mmap (other process).

    Same-process delete fails on Windows while the mmap is open; production
    calls this from Gunicorn ``worker_exit`` in the arbiter after the worker
    dies, so the cleaner is a separate process.
    """
    multiproc_dir = str(tmp_path)
    writer = _run_child(
        """
        import os
        from fastapi_vitals.metrics import HTTP_REQUESTS_IN_FLIGHT, identity_labels

        svc, env, _ver = identity_labels()
        HTTP_REQUESTS_IN_FLIGHT.labels(svc, env, "GET", "/ping").inc()
        print(os.getpid())
        """,
        multiproc_dir=multiproc_dir,
    )
    _assert_ok(writer)
    dead_pid = int(writer.stdout.strip())
    assert (tmp_path / f"gauge_livesum_{dead_pid}.db").exists()

    cleaner = _run_child(
        f"""
        from pathlib import Path
        import os
        from fastapi_vitals.metrics import mark_process_dead, metrics_response

        mark_process_dead({dead_pid})
        assert not Path(os.environ["PROMETHEUS_MULTIPROC_DIR"]).joinpath(
            "gauge_livesum_{dead_pid}.db"
        ).exists()
        body = metrics_response().body.decode()
        assert "http_requests_in_flight{{" not in body
        print("ok")
        """,
        multiproc_dir=multiproc_dir,
    )
    _assert_ok(cleaner)
    assert "ok" in cleaner.stdout


def test_mark_process_dead_public_export() -> None:
    """mark_process_dead stays on the package public surface."""
    import fastapi_vitals.metrics as metrics_pkg
    from fastapi_vitals.metrics import mark_process_dead

    assert callable(mark_process_dead)
    assert "mark_process_dead" in metrics_pkg.__all__
    assert mark_process_dead is metrics_pkg.mark_process_dead


def test_mark_process_dead_without_multiproc_env_fail_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without multiproc env, mark_process_dead must not raise or warn."""
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
    from fastapi_vitals.metrics import mark_process_dead

    with caplog.at_level("WARNING", logger="fastapi_vitals.metrics.middleware"):
        mark_process_dead()
        mark_process_dead(os.getpid())
    assert not any("mark_process_dead" in r.message for r in caplog.records)


@pytest.mark.parametrize("blank", ["", "   "])
def test_mark_process_dead_blank_env_does_not_touch_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """Blank multiproc env must not glob/delete gauge_*.db relative to CWD."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", blank)
    monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
    bait = tmp_path / f"gauge_livesum_{os.getpid()}.db"
    bait.write_bytes(b"bait")
    from fastapi_vitals.metrics import mark_process_dead

    mark_process_dead(os.getpid())
    assert bait.exists()
