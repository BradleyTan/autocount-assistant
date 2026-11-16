"""Snapshot an account book's schema (tables, columns, relationships) into the knowledge base."""

from __future__ import annotations

from ..connectors.sqlserver import SqlServerConnector
from .store import KnowledgeStore


def learn_schema(sql: SqlServerConnector, store: KnowledgeStore, db_name: str) -> tuple[int, int]:
    """Returns (tables, relationships) saved. Only structure is read; no row data."""
    return store.add_schema(db_name, sql.schema())
