"""Tenant-isolated, read-only Legal Operations Sandbox."""

from app.legal_ops.repository import SandboxRepository
from app.legal_ops.service import LegalOpsReadService

__all__ = ["LegalOpsReadService", "SandboxRepository"]
