"""Toolkit-neutral application service used by headless clients."""

from .service import AmethystService, ServiceError

__all__ = ["AmethystService", "ServiceError"]
