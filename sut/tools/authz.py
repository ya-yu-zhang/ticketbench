"""Tenant authorization for fake tickets."""

from __future__ import annotations


class UnauthorizedError(PermissionError):
    """Raised when actor_tenant does not own the resource."""


def assert_same_tenant(actor_tenant: str, resource_tenant: str) -> None:
    if actor_tenant != resource_tenant:
        raise UnauthorizedError(
            f"actor_tenant={actor_tenant!r} cannot access tenant={resource_tenant!r}"
        )
