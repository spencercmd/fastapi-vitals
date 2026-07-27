"""Optional AWS ECS resource enrichment for OTEL.

Not required for tracing or metrics. Activates only when an ECS container
metadata URI is present in the environment. Fail-open: hung metadata
never blocks process start (0.5s timeout).

Never writes ``cloud.account.id``. Task id is the TaskARN last path
segment only (not the account mid-segment).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Mapping, Optional
from urllib.error import URLError
from urllib.request import urlopen

logger = logging.getLogger(__name__)

# Cap ECS metadata latency so cold start never blocks on a hung link-local hop.
_ECS_METADATA_TIMEOUT_S = 0.5

# Classic AZ suffix (e.g. us-west-2a → us-west-2). Prefer AWS_REGION when set.
_AZ_REGION_RE = re.compile(r"^(.+)[a-z]$")


def is_available() -> bool:
    """Return True when ECS metadata URI env vars indicate an ECS runtime."""
    return bool(ecs_metadata_base_url())


def ecs_metadata_base_url() -> Optional[str]:
    """Return ECS container metadata base URL when running on ECS."""
    return os.getenv("ECS_CONTAINER_METADATA_URI_V4") or os.getenv(
        "ECS_CONTAINER_METADATA_URI"
    )


def fetch_ecs_task_metadata() -> Optional[Mapping[str, Any]]:
    """GET {metadata}/task once. Fail open on any error (never raises)."""
    base = ecs_metadata_base_url()
    if not base:
        return None
    url = f"{base.rstrip('/')}/task"
    try:
        with urlopen(url, timeout=_ECS_METADATA_TIMEOUT_S) as resp:  # noqa: S310
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("ECS task metadata unavailable from %s: %s", url, exc)
        return None
    if not isinstance(data, dict):
        logger.debug("ECS task metadata from %s was not a JSON object", url)
        return None
    return data


def task_id_from_arn(task_arn: str) -> Optional[str]:
    """Extract ECS task id from a TaskARN (last path segment only).

    Never returns the account id embedded mid-ARN.
    """
    if not task_arn or not isinstance(task_arn, str):
        return None
    task_id = task_arn.rstrip("/").rsplit("/", 1)[-1].strip()
    return task_id or None


def region_from_availability_zone(az: str) -> Optional[str]:
    if not az or not isinstance(az, str):
        return None
    match = _AZ_REGION_RE.match(az.strip())
    return match.group(1) if match else None


def cloud_attributes(task_meta: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """cloud.provider/platform/region when on ECS. Never cloud.account.id."""
    if not ecs_metadata_base_url():
        return {}
    attrs: Dict[str, str] = {
        "cloud.provider": "aws",
        "cloud.platform": "aws_ecs",
    }
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if not region and task_meta is not None:
        az = task_meta.get("AvailabilityZone") or task_meta.get("availabilityZone")
        if isinstance(az, str):
            region = region_from_availability_zone(az)
    if region:
        attrs["cloud.region"] = region
    return attrs


def enrich() -> Dict[str, str]:
    """Return ECS cloud + preferred instance-id attributes, or empty dict.

    Safe to call off-ECS (returns ``{}``). Fail-open on metadata errors.
    """
    if not is_available():
        return {}
    task_meta = fetch_ecs_task_metadata()
    attrs = cloud_attributes(task_meta)
    task_id: Optional[str] = None
    if task_meta is not None:
        arn = task_meta.get("TaskARN") or task_meta.get("TaskArn")
        if isinstance(arn, str):
            task_id = task_id_from_arn(arn)
    if task_id:
        # Prefer task id over HOSTNAME when present (caller merges later).
        attrs["service.instance.id"] = task_id
    return attrs
