from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class UnsafeScopeError(ValueError):
    """Raised when a provider asks for anything beyond read-only mail access."""


def split_scopes(scopes: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(scopes, str):
        return tuple(scope for scope in scopes.split() if scope)
    return tuple(scope for scope in scopes if scope)


def _canonical(scope: str) -> str:
    return scope.strip().lower()


@dataclass(frozen=True)
class ScopePolicy:
    provider: str
    allowed_resource_scopes: frozenset[str]
    required_resource_scope: str

    @property
    def _allowed(self) -> frozenset[str]:
        return frozenset(_canonical(scope) for scope in self.allowed_resource_scopes)

    def validate_requested(self, scopes: str | Iterable[str]) -> None:
        requested = split_scopes(scopes)
        self._validate(requested, "requested")

    def validate_granted(self, scopes: str | Iterable[str]) -> None:
        granted = split_scopes(scopes)
        self._validate(granted, "granted")

    def _validate(self, scopes: Iterable[str], source: str) -> None:
        canonical_scopes = {_canonical(scope) for scope in scopes}
        required = self._allowed

        if not canonical_scopes.intersection(required):
            raise UnsafeScopeError(
                f"{self.provider} {source} scopes must include "
                f"{self.required_resource_scope!r}."
            )

        unexpected = sorted(scope for scope in canonical_scopes if scope not in self._allowed)
        if unexpected:
            joined = ", ".join(unexpected)
            raise UnsafeScopeError(
                f"{self.provider} {source} unexpected mail scope(s): {joined}. "
                "Only the configured read-only mail scope is allowed."
            )


GMAIL_SCOPE_POLICY = ScopePolicy(
    provider="gmail",
    allowed_resource_scopes=frozenset({GMAIL_READONLY_SCOPE}),
    required_resource_scope=GMAIL_READONLY_SCOPE,
)
