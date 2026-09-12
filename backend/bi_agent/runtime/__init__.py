"""Contracts and persistence abstractions for deterministic query runs."""

from .memory import MemoryQueryRunStore
from .repository import PostgresQueryRunStore
from .models import (
    ArtifactPersistenceError,
    ArtifactRef,
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    NewQueryRun,
    QueryRunStore,
    RecoveryAction,
    RunCompletion,
    RunContextNotFound,
    RunEventType,
    RunNotFound,
    RunStatus,
    RunTransition,
    SchemaOutdated,
    StaleRunRevision,
    TurnContext,
)

__all__ = [
    "ArtifactPersistenceError",
    "ArtifactRef",
    "DomainArtifact",
    "DomainResult",
    "DomainStatus",
    "ErrorEnvelope",
    "MemoryQueryRunStore",
    "PostgresQueryRunStore",
    "NewArtifact",
    "NewQueryRun",
    "QueryRunStore",
    "RecoveryAction",
    "RunCompletion",
    "RunContextNotFound",
    "RunEventType",
    "RunNotFound",
    "RunStatus",
    "RunTransition",
    "SchemaOutdated",
    "StaleRunRevision",
    "TurnContext",
]
