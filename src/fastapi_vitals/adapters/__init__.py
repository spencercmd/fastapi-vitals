"""Optional runtime adapters (cloud metadata, etc.).

Core setup works without any adapter. Import and register adapters only
when you need platform-specific resource enrichment.
"""

from __future__ import annotations

from . import ecs

__all__ = ["ecs"]
