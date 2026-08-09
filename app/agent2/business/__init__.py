"""Deterministic business domains owned by Agent2.

This package never accepts natural-language text at an executor boundary. Semantic
candidates must first be canonicalized into the typed commands exported here.
"""

from app.agent2.business.contracts import BusinessCommandContext, BusinessReceipt
from app.agent2.business.executor import InMemoryBusinessExecutor

__all__ = ["BusinessCommandContext", "BusinessReceipt", "InMemoryBusinessExecutor"]
