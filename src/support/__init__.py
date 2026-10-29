"""Support: A modular, extensible, multi-database natural-language-to-SQL application."""

from support.__version__ import __version__
from support.core.dialect import Dialect
from support.schema.models import DatabaseSchema, TableSchema, ColumnSchema

__all__ = ["__version__", "Dialect", "DatabaseSchema", "TableSchema", "ColumnSchema"]
