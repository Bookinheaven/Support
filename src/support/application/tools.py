"""Database intelligence tool definitions, registry, argument validation, and dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from support.application.connection import ConnectionTestWorkflow
from support.application.errors import (
    AmbiguousObjectNameError,
    InvalidToolArgumentsError,
    ObjectNotFoundError,
    UnknownToolError,
)
from support.application.schema import SchemaIntrospectionWorkflow
from support.domain.assistant import ToolDefinition
from support.domain.profiles import DatabaseType, SQLiteConnection
from support.domain.schema import DatabaseSchema, TableMetadata, TableType


@dataclass(frozen=True)
class ToolResult:
    """Outcome of executing an approved application database tool."""

    name: str
    call_id: str
    data: dict[str, Any]
    raw_object: Any | None = None
    is_error: bool = False
    message: str = ""
    response_mode: str = "display"
    presentation_options: dict[str, Any] = field(default_factory=dict)


def extract_presentation_options(arguments: dict[str, Any]) -> dict[str, bool]:
    """Extract and validate presentation preferences from structured tool arguments."""
    nested = arguments.get("presentation_options")
    opts = dict(nested) if isinstance(nested, dict) else {}

    for k in ("include_schema", "names_only", "numbered", "alphabetical"):
        if k in arguments and k not in opts:
            opts[k] = arguments[k]

    if "omit_schema" in arguments and "include_schema" not in opts:
        opts["include_schema"] = not bool(arguments["omit_schema"])
    elif "omit_schema" in opts and "include_schema" not in opts:
        opts["include_schema"] = not bool(opts["omit_schema"])

    inc_schema = bool(opts.get("include_schema", True))
    return {
        "include_schema": inc_schema,
        "omit_schema": not inc_schema,
        "names_only": bool(opts.get("names_only", False)),
        "numbered": bool(opts.get("numbered", False)),
        "alphabetical": bool(opts.get("alphabetical", False)),
    }


# -----------------------------------------------------------------------------
# Explicit tool definitions
# -----------------------------------------------------------------------------

TOOL_LIST_DATABASE_OBJECTS = ToolDefinition(
    name="list_database_objects",
    description="This tool lists metadata about tables, views or collections in the active database. It does not read their records or table rows.",
    parameters={
        "type": "object",
        "properties": {
            "object_type": {
                "type": "string",
                "enum": ["all", "table", "view", "collection"],
                "description": "Optional filter: 'all', 'table', 'view', or 'collection'. Default is 'all'.",
            },
            "response_mode": {
                "type": "string",
                "enum": ["display", "explain"],
                "description": "Response mode: 'display' (default) to render tables directly, or 'explain' to analyze.",
            },
            "presentation_options": {
                "type": "object",
                "description": "Optional presentation preferences for displaying results.",
                "properties": {
                    "include_schema": {
                        "type": "boolean",
                        "description": "Whether to include schema prefix in table names (e.g. 'public.orders' vs 'orders'). Set to false if user requests omitting schema or public prefix. Default is true.",
                    },
                    "names_only": {
                        "type": "boolean",
                        "description": "Whether to display only object names without headers or summary counts. Default is false.",
                    },
                    "numbered": {
                        "type": "boolean",
                        "description": "Whether to number the listed objects (e.g. 1. orders). Default is false.",
                    },
                    "alphabetical": {
                        "type": "boolean",
                        "description": "Whether to sort objects alphabetically. Default is false.",
                    },
                },
            },
        },
    },
)

TOOL_GET_DATABASE_OVERVIEW = ToolDefinition(
    name="get_database_overview",
    description="Retrieve a high-level structural overview and summary counts for the active database. It does not read or return table records.",
    parameters={
        "type": "object",
        "properties": {
            "response_mode": {
                "type": "string",
                "enum": ["display", "explain"],
                "description": "Response mode: 'display' (default) or 'explain'.",
            },
        },
    },
)

TOOL_DESCRIBE_OBJECT = ToolDefinition(
    name="describe_object",
    description="This tool retrieves metadata describing the structure of a database object. It does not retrieve application records or row contents.",
    parameters={
        "type": "object",
        "properties": {
            "object_name": {
                "type": "string",
                "description": "The exact name of the table, view, or collection to describe.",
            },
            "schema": {
                "type": "string",
                "description": "Optional schema or namespace name to qualify the object lookup.",
            },
            "response_mode": {
                "type": "string",
                "enum": ["display", "explain"],
                "description": "Response mode: 'display' (default) to render details, or 'explain' to analyze.",
            },
        },
        "required": ["object_name"],
    },
)

TOOL_GET_RELATIONSHIPS = ToolDefinition(
    name="get_relationships",
    description=(
        "This tool retrieves declared foreign-key relationships. It must not infer relationships merely from "
        "similar column names and does not read table records. Always call this tool when asked about relationships, "
        "foreign keys, or references between tables (including why or how tables reference each other)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "Optional table name to filter relationships referencing or belonging to this table.",
            },
            "response_mode": {
                "type": "string",
                "enum": ["display", "explain"],
                "description": "Response mode: 'display' (default) to render relationships, or 'explain' to explain connections.",
            },
            "presentation_options": {
                "type": "object",
                "description": "Optional presentation preferences for displaying relationships.",
                "properties": {
                    "names_only": {
                        "type": "boolean",
                        "description": "Whether to display only table names without extra headers. Default is false.",
                    },
                },
            },
        },
    },
)

TOOL_GET_CONNECTION_STATUS = ToolDefinition(
    name="get_connection_status",
    description="Retrieve current active database connection status and engine details without secrets. It does not inspect database contents.",
    parameters={
        "type": "object",
        "properties": {
            "response_mode": {
                "type": "string",
                "enum": ["display", "explain"],
                "description": "Response mode: 'display' (default) or 'explain'.",
            },
        },
    },
)

APPROVED_DATABASE_TOOLS: tuple[ToolDefinition, ...] = (
    TOOL_LIST_DATABASE_OBJECTS,
    TOOL_GET_DATABASE_OVERVIEW,
    TOOL_DESCRIBE_OBJECT,
    TOOL_GET_RELATIONSHIPS,
    TOOL_GET_CONNECTION_STATUS,
)


class ToolRegistry:
    """Registry managing approved database tools and orchestrating authorized execution."""

    def __init__(
        self,
        schema_workflow: SchemaIntrospectionWorkflow,
        connection_workflow: ConnectionTestWorkflow,
    ) -> None:
        self._schema_workflow = schema_workflow
        self._connection_workflow = connection_workflow
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in APPROVED_DATABASE_TOOLS}

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return list of all approved tool schemas."""
        return list(self._tools.values())

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        active_profile: str | None,
        call_id: str = "",
    ) -> ToolResult:
        """Validate and execute an approved database tool.

        Never permits arbitrary execution or execution outside registered tools.
        """
        if tool_name not in self._tools:
            return ToolResult(
                name=tool_name,
                call_id=call_id,
                data={"error": f"Unknown tool '{tool_name}'. Only registered database tools may be executed."},
                is_error=True,
                message=f"Unknown tool: {tool_name}",
            )

        if not active_profile or active_profile == "(none)":
            return ToolResult(
                name=tool_name,
                call_id=call_id,
                data={
                    "error": (
                        "No active database connection is configured. "
                        "Please select or add a database connection before querying database structure."
                    )
                },
                is_error=True,
                message="No active database connection.",
            )

        handler_map = {
            "list_database_objects": self._handle_list_database_objects,
            "get_database_overview": self._handle_get_database_overview,
            "describe_object": self._handle_describe_object,
            "get_relationships": self._handle_get_relationships,
            "get_connection_status": self._handle_get_connection_status,
        }

        handler = handler_map.get(tool_name)
        if handler is None:
            return ToolResult(
                name=tool_name,
                call_id=call_id,
                data={"error": f"Tool '{tool_name}' has no registered execution handler."},
                is_error=True,
            )

        mode_arg = arguments.get("response_mode")
        if mode_arg is not None:
            if not isinstance(mode_arg, str) or mode_arg.strip().lower() not in ("display", "explain"):
                return ToolResult(
                    name=tool_name,
                    call_id=call_id,
                    data={"error": f"Invalid response_mode '{mode_arg}'. Must be 'display' or 'explain'."},
                    is_error=True,
                    message="Invalid response_mode",
                )
            resolved_mode = mode_arg.strip().lower()
        else:
            resolved_mode = "display"

        pres_opts = extract_presentation_options(arguments)

        try:
            res = handler(arguments, active_profile, call_id)
            return ToolResult(
                name=res.name,
                call_id=res.call_id,
                data=res.data,
                raw_object=res.raw_object,
                is_error=res.is_error,
                message=res.message,
                response_mode=resolved_mode if not res.is_error else res.response_mode,
                presentation_options=pres_opts,
            )
        except Exception as exc:
            return ToolResult(
                name=tool_name,
                call_id=call_id,
                data={"error": str(exc)},
                is_error=True,
                message=f"Tool execution failed: {exc}",
                response_mode=resolved_mode,
                presentation_options=pres_opts,
            )

    # -------------------------------------------------------------------------
    # Tool Handlers
    # -------------------------------------------------------------------------

    def _handle_list_database_objects(
        self,
        arguments: dict[str, Any],
        active_profile: str,
        call_id: str,
    ) -> ToolResult:
        obj_type = (arguments.get("object_type") or "all").lower()
        if obj_type not in ("all", "table", "view", "collection"):
            return ToolResult(
                name="list_database_objects",
                call_id=call_id,
                data={"error": f"Invalid object_type '{obj_type}'. Must be 'all', 'table', 'view', or 'collection'."},
                is_error=True,
            )

        schema: DatabaseSchema = self._schema_workflow.get_schema(active_profile)
        if schema.engine.lower() == "mongodb":
            names = [t.name for t in schema.tables]
            return ToolResult(
                name="list_database_objects",
                call_id=call_id,
                data={
                    "database": schema.database_name,
                    "engine": "MongoDB",
                    "collections": names,
                    "count": len(names),
                },
                raw_object=schema,
            )

        tables = [t.qualified_name for t in schema.tables if t.table_type == TableType.TABLE]
        views = [t.qualified_name for t in schema.tables if t.table_type == TableType.VIEW]

        data: dict[str, Any] = {
            "database": schema.database_name,
            "engine": schema.engine,
        }
        if obj_type in ("all", "table"):
            data["tables"] = tables
            data["table_count"] = len(tables)
        if obj_type in ("all", "view"):
            data["views"] = views
            data["view_count"] = len(views)

        return ToolResult(
            name="list_database_objects",
            call_id=call_id,
            data=data,
            raw_object=schema,
        )

    def _handle_get_database_overview(
        self,
        arguments: dict[str, Any],
        active_profile: str,
        call_id: str,
    ) -> ToolResult:
        schema: DatabaseSchema = self._schema_workflow.get_schema(active_profile)
        if schema.engine.lower() == "mongodb":
            return ToolResult(
                name="get_database_overview",
                call_id=call_id,
                data={
                    "database": schema.database_name,
                    "engine": "MongoDB",
                    "collections_count": len(schema.tables),
                    "total_indexes": sum(len(t.indexes) for t in schema.tables),
                },
                raw_object=schema,
            )

        tables = [t for t in schema.tables if t.table_type == TableType.TABLE]
        views = [t for t in schema.tables if t.table_type == TableType.VIEW]

        return ToolResult(
            name="get_database_overview",
            call_id=call_id,
            data={
                "database": schema.database_name,
                "engine": schema.engine,
                "schemas_count": len(schema.schemas),
                "tables_count": len(tables),
                "views_count": len(views),
                "relationships_count": len(schema.relationships),
                "total_indexes": sum(len(t.indexes) for t in schema.tables),
            },
            raw_object=schema,
        )

    def _handle_describe_object(
        self,
        arguments: dict[str, Any],
        active_profile: str,
        call_id: str,
    ) -> ToolResult:
        obj_name = arguments.get("object_name")
        if not obj_name or not isinstance(obj_name, str) or not obj_name.strip():
            return ToolResult(
                name="describe_object",
                call_id=call_id,
                data={"error": "Parameter 'object_name' is required and cannot be empty."},
                is_error=True,
            )

        schema_arg = arguments.get("schema")
        if schema_arg and isinstance(schema_arg, str):
            schema_arg = schema_arg.strip() or None
        else:
            schema_arg = None

        try:
            tbl_meta: TableMetadata = self._schema_workflow.describe_object(
                active_profile,
                obj_name.strip(),
                schema=schema_arg,
            )
        except ObjectNotFoundError as exc:
            return ToolResult(
                name="describe_object",
                call_id=call_id,
                data={"error": f"Object '{obj_name}' does not exist in the database."},
                is_error=True,
                message=str(exc),
            )
        except AmbiguousObjectNameError as exc:
            return ToolResult(
                name="describe_object",
                call_id=call_id,
                data={
                    "error": (
                        f"Object '{obj_name}' exists in multiple schemas and is ambiguous. "
                        "Please specify the 'schema' parameter (e.g. schema='public')."
                    )
                },
                is_error=True,
                message=str(exc),
            )

        profile = self._schema_workflow.get_profile(active_profile)
        engine = profile.connection.database_type.value

        if engine.lower() == "mongodb":
            data = {
                "collection": tbl_meta.name,
                "engine": "MongoDB",
                "options": dict(tbl_meta.options),
                "validator": dict(tbl_meta.validator) if tbl_meta.validator else None,
                "indexes": [
                    {
                        "name": idx.name,
                        "fields": list(idx.columns),
                        "orders": list(idx.column_orders),
                        "unique": idx.unique,
                        "filter": idx.filter_condition,
                    }
                    for idx in tbl_meta.indexes
                ],
            }
        else:
            data = {
                "name": tbl_meta.qualified_name,
                "type": tbl_meta.table_type.value,
                "engine": engine,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "nullable": col.nullable,
                        "default": col.default,
                        "is_primary": bool(tbl_meta.primary_key and col.name in tbl_meta.primary_key.columns),
                    }
                    for col in tbl_meta.columns
                ],
                "primary_key": list(tbl_meta.primary_key.columns) if tbl_meta.primary_key else None,
                "foreign_keys": [
                    {
                        "name": fk.name,
                        "source_columns": list(fk.source_columns),
                        "target_table": fk.target_table,
                        "target_columns": list(fk.target_columns),
                        "on_delete": fk.on_delete,
                    }
                    for fk in tbl_meta.foreign_keys
                ],
                "unique_constraints": [
                    {"name": u.name, "columns": list(u.columns)}
                    for u in tbl_meta.unique_constraints
                ],
                "check_constraints": [
                    {"name": c.name, "clause": c.check_clause}
                    for c in tbl_meta.check_constraints
                ],
                "indexes": [
                    {
                        "name": idx.name,
                        "columns": list(idx.columns),
                        "orders": list(idx.column_orders),
                        "included": list(idx.included_columns),
                        "unique": idx.unique,
                        "method": idx.method,
                        "filter": idx.filter_condition,
                    }
                    for idx in tbl_meta.indexes
                ],
            }

        return ToolResult(
            name="describe_object",
            call_id=call_id,
            data=data,
            raw_object=tbl_meta,
        )

    def _handle_get_relationships(
        self,
        arguments: dict[str, Any],
        active_profile: str,
        call_id: str,
    ) -> ToolResult:
        schema: DatabaseSchema = self._schema_workflow.get_schema(active_profile)
        if schema.engine.lower() == "mongodb":
            return ToolResult(
                name="get_relationships",
                call_id=call_id,
                data={
                    "database": schema.database_name,
                    "engine": "MongoDB",
                    "relationships": [],
                    "message": "MongoDB collections do not define relational foreign-key constraints.",
                },
                raw_object=schema,
            )

        filter_table = arguments.get("table_name")
        if filter_table and isinstance(filter_table, str):
            filter_table = filter_table.strip().lower()
        else:
            filter_table = None

        relationships = []
        for fk in schema.relationships:
            if filter_table:
                src_match = (fk.source_table.lower() == filter_table)
                tgt_match = (fk.target_table.lower() == filter_table)
                if not (src_match or tgt_match):
                    continue

            relationships.append(
                {
                    "name": fk.name,
                    "source": fk.source_display,
                    "target": fk.target_display,
                    "source_table": fk.source_table,
                    "source_columns": list(fk.source_columns),
                    "target_table": fk.target_table,
                    "target_columns": list(fk.target_columns),
                    "on_delete": fk.on_delete,
                    "on_update": fk.on_update,
                }
            )

        return ToolResult(
            name="get_relationships",
            call_id=call_id,
            data={
                "database": schema.database_name,
                "engine": schema.engine,
                "filter_table": filter_table,
                "relationships": relationships,
                "count": len(relationships),
            },
            raw_object=schema,
        )

    def _handle_get_connection_status(
        self,
        arguments: dict[str, Any],
        active_profile: str,
        call_id: str,
    ) -> ToolResult:
        profile = self._connection_workflow.get_profile(active_profile)
        conn = profile.connection
        db_type = conn.database_type.value

        data = {
            "profile_name": profile.name,
            "database_type": db_type,
            "database": getattr(conn, "database", None) or getattr(conn, "database_path", None),
            "host": getattr(conn, "host", None),
            "port": getattr(conn, "port", None),
            "username": getattr(conn, "username", None),
        }

        return ToolResult(
            name="get_connection_status",
            call_id=call_id,
            data=data,
        )
