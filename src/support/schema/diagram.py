"""Reliable, deterministic database ER diagram generation using schema metadata.

Outputs valid Mermaid erDiagram source (.mmd) and optional interactive HTML viewers.
Supports PostgreSQL, MySQL, SQLite, and SQL Server with foreign-key-grounded cardinalities.
"""

import os
import re
from pathlib import Path
from typing import Optional, Set

from support.core.dialect import Dialect
from support.core.exceptions import SupportError
from support.schema.models import ColumnSchema, DatabaseSchema, ForeignKeySchema, TableSchema


def sanitize_mermaid_identifier(name: str) -> str:
    """Normalize a table or column name into a valid, safe Mermaid identifier.

    Replaces spaces, hyphens, dots, and special characters with underscores.
    """
    if not name:
        return "unnamed"
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    # Ensure identifier does not begin with a digit
    if clean and clean[0].isdigit():
        clean = f"t_{clean}"
    return clean or "unnamed"


def sanitize_type_for_mermaid(raw_type: str) -> str:
    """Normalize SQL data type into a clean, single-word Mermaid ER attribute type identifier."""
    if not raw_type or not raw_type.strip():
        return "string"

    t = raw_type.strip().lower()
    # Strip precision/length in parentheses e.g. varchar(255) -> varchar
    t = re.sub(r"\(.*?\)", "", t).strip()

    if "int" in t:
        if "big" in t:
            return "bigint"
        if "small" in t:
            return "smallint"
        if "tiny" in t:
            return "tinyint"
        return "int"
    if "char" in t or "text" in t or "clob" in t or "string" in t:
        return "varchar" if "var" in t else ("text" if "text" in t else "string")
    if "bool" in t or t == "bit":
        return "boolean"
    if "time" in t or "date" in t:
        if "tz" in t or "zone" in t:
            return "timestamptz"
        if "date" in t and "time" not in t:
            return "date"
        return "datetime"
    if "dec" in t or "num" in t:
        return "numeric"
    if "float" in t or "double" in t or "real" in t:
        return "float"
    if "json" in t:
        return "json"
    if "uuid" in t or "uniqueidentifier" in t:
        return "uuid"
    if "blob" in t or "byte" in t or "binary" in t:
        return "binary"

    clean = re.sub(r"[^a-zA-Z0-9_]", "_", t)
    return clean or "string"


def _is_foreign_key_unique(child_table: TableSchema, fk: ForeignKeySchema) -> bool:
    """Determine whether a foreign key constraint represents a 1-to-1 relationship."""
    if not fk.constrained_columns:
        return False

    fk_cols_lower = [c.lower() for c in fk.constrained_columns]

    # Check 1: Foreign key columns comprise the child table's primary key
    if child_table.primary_key:
        pk_cols_lower = [c.lower() for c in child_table.primary_key]
        if sorted(fk_cols_lower) == sorted(pk_cols_lower):
            return True

    # Check 2: Single-column FK where column has is_unique=True
    if len(fk.constrained_columns) == 1:
        col = child_table.get_column(fk.constrained_columns[0])
        if col and col.is_unique:
            return True

    # Check 3: Unique constraint matching FK columns exactly
    for uq in child_table.unique_constraints:
        uq_cols_lower = [c.lower() for c in uq.columns]
        if sorted(fk_cols_lower) == sorted(uq_cols_lower):
            return True

    return False


def _is_foreign_key_nullable(child_table: TableSchema, fk: ForeignKeySchema) -> bool:
    """Determine whether any column in a foreign key is nullable."""
    for col_name in fk.constrained_columns:
        col = child_table.get_column(col_name)
        if col and col.nullable is not False:
            return True
    return False


def resolve_selected_tables_with_paths(
    schema: DatabaseSchema,
    requested_names: list[str],
) -> list[TableSchema]:
    """Resolve requested table names and preserve relationship paths/junction tables between them."""
    if not requested_names:
        return list(schema.tables.values())

    req_lower = {name.strip().lower() for name in requested_names if name.strip()}
    matched_tables: dict[str, TableSchema] = {}

    for t_name, table in schema.tables.items():
        if t_name.lower() in req_lower or table.name.lower() in req_lower:
            matched_tables[table.name.lower()] = table

    # Include intermediate/junction tables bridging requested tables
    if len(matched_tables) >= 2:
        for t_name, candidate in schema.tables.items():
            c_lower = candidate.name.lower()
            if c_lower in matched_tables:
                continue

            # Build neighbors connected via foreign keys in either direction
            neighbors = set()
            for fk in candidate.foreign_keys:
                neighbors.add(fk.referred_table.lower())
            for other in schema.tables.values():
                for ofk in other.foreign_keys:
                    if ofk.referred_table.lower() == c_lower:
                        neighbors.add(other.name.lower())

            # Check how many requested matched tables this candidate connects to
            if len(neighbors.intersection(matched_tables.keys())) >= 2:
                matched_tables[c_lower] = candidate

    return list(matched_tables.values())


def generate_mermaid_er(
    schema: DatabaseSchema,
    table_filter: Optional[list[str]] = None,
    title: Optional[str] = None,
) -> str:
    """Generate deterministic, valid Mermaid erDiagram source from database schema metadata.

    Args:
        schema: Discovered DatabaseSchema object.
        table_filter: Optional subset of table names to include.
        title: Optional diagram title or header comment.

    Returns:
        String containing valid Mermaid erDiagram code.
    """
    if schema.dialect == Dialect.MONGODB:
        raise SupportError(
            "Relational ER diagrams are not applicable to MongoDB. "
            "MongoDB document collections do not enforce relational foreign keys."
        )

    lines: list[str] = ["erDiagram"]

    # Header comments
    db_name = getattr(schema, "database_name", None) or "database"
    dialect_str = schema.dialect.value if hasattr(schema.dialect, "value") else str(schema.dialect)
    header = title or f"Database: {db_name} ({dialect_str})"
    lines.append(f"    %% {header}")

    # Determine tables to render
    if table_filter:
        tables_to_render = resolve_selected_tables_with_paths(schema, table_filter)
    else:
        tables_to_render = list(schema.tables.values())

    if not tables_to_render:
        lines.append("    %% No tables found in database schema")
        return "\n".join(lines) + "\n"

    # Map table names to sanitized Mermaid identifiers
    name_to_id: dict[str, str] = {}
    for table in tables_to_render:
        name_to_id[table.name.lower()] = sanitize_mermaid_identifier(table.name)

    # 1. Render Table Entities
    for table in sorted(tables_to_render, key=lambda t: t.name.lower()):
        t_id = name_to_id[table.name.lower()]
        lines.append(f"    {t_id} {{")

        # Set of foreign key columns in this table
        fk_cols: Set[str] = set()
        for fk in table.foreign_keys:
            for col in fk.constrained_columns:
                fk_cols.add(col.lower())

        pk_cols = {c.lower() for c in table.primary_key}

        # Render columns
        if not table.columns:
            lines.append("        string placeholder")
        else:
            for col in table.columns:
                c_name = sanitize_mermaid_identifier(col.name)
                c_type = sanitize_type_for_mermaid(col.data_type)
                c_lower = col.name.lower()

                is_pk = c_lower in pk_cols or col.is_primary_key
                is_fk = c_lower in fk_cols
                is_uk = col.is_unique

                key_parts = []
                if is_pk:
                    key_parts.append("PK")
                if is_fk:
                    key_parts.append("FK")
                if is_uk and not is_pk:
                    key_parts.append("UK")

                key_str = f" {','.join(key_parts)}" if key_parts else ""

                comment_str = ""
                if col.nullable is True and not is_pk:
                    comment_str = ' "nullable"'

                lines.append(f"        {c_type} {c_name}{key_str}{comment_str}")

        lines.append("    }")

    # 2. Render Relationships from Discovered Foreign Keys
    rendered_relationships: Set[str] = set()

    for table in sorted(tables_to_render, key=lambda t: t.name.lower()):
        child_id = name_to_id[table.name.lower()]

        for fk in table.foreign_keys:
            parent_name_lower = fk.referred_table.lower()
            if parent_name_lower not in name_to_id:
                # Parent table is not in the rendered subset
                continue

            parent_id = name_to_id[parent_name_lower]

            is_1to1 = _is_foreign_key_unique(table, fk)
            is_nullable = _is_foreign_key_nullable(table, fk)

            # Left side (Parent): Parent row is identified by PK (exactly 1 parent)
            parent_side = "||"

            # Right side (Child):
            if is_1to1:
                child_side = "o|" if is_nullable else "||"
            else:
                child_side = "o{" if is_nullable else "|{"

            # Relationship label derived from constrained column(s)
            fk_label = ", ".join(fk.constrained_columns) if fk.constrained_columns else "references"
            # Sanitize label for Mermaid string
            clean_label = re.sub(r'["\n]', "", fk_label)

            rel_signature = f"{parent_id}_{child_id}_{clean_label}"
            if rel_signature in rendered_relationships:
                continue
            rendered_relationships.add(rel_signature)

            lines.append(f'    {parent_id} {parent_side}--{child_side} {child_id} : "{clean_label}"')

    return "\n".join(lines) + "\n"


def get_default_diagram_path(profile_name: str) -> Path:
    """Return default path for diagram files under ~/.support/diagrams/."""
    env_dir = os.getenv("SUPPORT_CONFIG_DIR")
    base_dir = Path(env_dir) if env_dir else Path.home() / ".support"
    diagrams_dir = base_dir / "diagrams"
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    clean_name = sanitize_mermaid_identifier(profile_name)
    return diagrams_dir / f"{clean_name}.mmd"


def save_diagram_file(
    mermaid_code: str,
    output_path: Optional[Path] = None,
    profile_name: str = "database",
    overwrite: bool = False,
    generate_html: bool = False,
) -> tuple[Path, Optional[Path]]:
    """Save Mermaid ER diagram to disk with overwrite protection and optional HTML viewer.

    Returns:
        (mmd_file_path, html_file_path_or_none)
    """
    target_path = Path(output_path) if output_path else get_default_diagram_path(profile_name)

    if target_path.exists() and not overwrite:
        raise FileExistsError(
            f"File '{target_path}' already exists. Use --overwrite (-y) to replace it."
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(mermaid_code, encoding="utf-8")

    html_path: Optional[Path] = None
    if generate_html:
        html_path = target_path.with_suffix(".html")
        html_content = _build_html_diagram_viewer(mermaid_code, title=f"ER Diagram · {profile_name}")
        html_path.write_text(html_content, encoding="utf-8")

    return target_path, html_path


def _build_html_diagram_viewer(mermaid_code: str, title: str) -> str:
    """Generate a clean, standalone, zero-dependency HTML viewer for the diagram."""
    clean_code = mermaid_code.strip()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
  <script>
    mermaid.initialize({{
      startOnLoad: true,
      theme: 'dark',
      er: {{
        useMaxWidth: false,
        layoutDirection: 'TB'
      }}
    }});
  </script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: #0b0f19;
      color: #f1f5f9;
      margin: 0;
      padding: 2rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .header {{
      width: 100%;
      max-width: 1200px;
      margin-bottom: 1.5rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid #1e293b;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    h1 {{
      font-size: 1.5rem;
      margin: 0;
      color: #38bdf8;
    }}
    .badge {{
      background: #1e293b;
      color: #94a3b8;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.875rem;
    }}
    .diagram-container {{
      width: 100%;
      max-width: 1200px;
      background: #0f172a;
      border: 1px solid #1e293b;
      border-radius: 12px;
      padding: 2rem;
      box-sizing: border-box;
      overflow-x: auto;
      display: flex;
      justify-content: center;
    }}
    .mermaid {{
      font-size: 1rem;
    }}
  </style>
</head>
<body>
  <div class="header">
    <h1>{title}</h1>
    <span class="badge">Mermaid erDiagram</span>
  </div>
  <div class="diagram-container">
    <div class="mermaid">
{clean_code}
    </div>
  </div>
</body>
</html>
"""
