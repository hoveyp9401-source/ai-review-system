"""Deterministic business domains owned by Agent2.

This package never accepts natural-language text at an executor boundary. Semantic
candidates must first be canonicalized into the typed commands exported here.
"""

from typing import TYPE_CHECKING, Any

from app.agent2.business.contracts import BusinessCommandContext, BusinessReceipt

if TYPE_CHECKING:
    from app.agent2.business.executor import InMemoryBusinessExecutor


def __getattr__(name: str) -> Any:
    # Keep the convenience export without importing the Executor while
    # admission_store is importing business.contracts.  The eager import made
    # the authoritative Ticket store depend on import order.
    if name == "InMemoryBusinessExecutor":
        from app.agent2.business.executor import InMemoryBusinessExecutor

        return InMemoryBusinessExecutor
    raise AttributeError(name)

__all__ = ["BusinessCommandContext", "BusinessReceipt", "InMemoryBusinessExecutor"]
