"""Registry of focused database tools for direct LLM and conversational interaction."""

from typing import Any
from support.tools.models import ToolDefinition

DATABASE_TOOLS: dict[str, ToolDefinition] = {
    "list_tables": ToolDefinition(
        name="list_tables",
        description="List all accessible tables, views, and schemas discovered in the active database.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    "inspect_tables": ToolDefinition(
        name="inspect_tables",
        description="Inspect and sample rows from one or more database tables with safe row limits and sensitive-field masking.",
        parameters={
            "type": "object",
            "properties": {
                "target_tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of specific table names or schema-qualified table names to sample rows from.",
                },
                "row_limit": {
                    "type": "integer",
                    "description": "Maximum number of rows to retrieve per table (default: 1, max: 10).",
                    "default": 1,
                },
                "all_tables": {
                    "type": "boolean",
                    "description": "Set to true to sample rows across all tables in the active database.",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    "generate_diagram": ToolDefinition(
        name="generate_diagram",
        description="Generate a deterministic entity-relationship (ER) diagram for the active database using live foreign-key metadata.",
        parameters={
            "type": "object",
            "properties": {
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of tables to include in the ER diagram.",
                },
            },
            "required": [],
        },
    ),
    "query_database": ToolDefinition(
        name="query_database",
        description="Retrieve or analyze database data using a natural-language question. Generates and validates a read-only SQL query draft for user approval and execution.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The natural language question or data retrieval request.",
                },
            },
            "required": ["query"],
        },
    ),
    "create_record": ToolDefinition(
        name="create_record",
        description="Create and insert a new record into a database table. Supports user-specified fields, deterministic derivation, and generating plausible sample data when explicitly authorized.",
        parameters={
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "The target table name (e.g. 'users', 'tasks', or 'public.users').",
                },
                "values": {
                    "type": "object",
                    "description": "Explicit key-value mappings for table columns to set.",
                    "default": {},
                },
                "allow_sample_data": {
                    "type": "boolean",
                    "description": "Set to true if the user explicitly authorized random, dummy, or sample details.",
                    "default": False,
                },
            },
            "required": ["table_name"],
        },
    ),
    "update_records": ToolDefinition(
        name="update_records",
        description="Modify existing records in a database table matching specific filter conditions. Unconditional updates are strictly prohibited.",
        parameters={
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "The target table name to update.",
                },
                "values": {
                    "type": "object",
                    "description": "Key-value mappings for table columns to update.",
                },
                "conditions": {
                    "type": "object",
                    "description": "Mandatory filter criteria identifying the records to update (e.g. {'id': 42}).",
                },
            },
            "required": ["table_name", "values", "conditions"],
        },
    ),
    "delete_records": ToolDefinition(
        name="delete_records",
        description="Delete existing records from a database table matching specific filter conditions. Unconditional deletes are strictly prohibited.",
        parameters={
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "The target table name to delete from.",
                },
                "conditions": {
                    "type": "object",
                    "description": "Mandatory filter criteria identifying the records to delete (e.g. {'id': 42}).",
                },
            },
            "required": ["table_name", "conditions"],
        },
    ),
    "explain_error": ToolDefinition(
        name="explain_error",
        description="Explain a database execution error, constraint violation, or query failure to the user with actionable guidance.",
        parameters={
            "type": "object",
            "properties": {
                "error_message": {
                    "type": "string",
                    "description": "The database error message or problem description to explain.",
                },
            },
            "required": ["error_message"],
        },
    ),
    "ask_clarification": ToolDefinition(
        name="ask_clarification",
        description="Ask the user to clarify an ambiguous request or provide missing required values that cannot be safely derived or generated.",
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The clarifying question to present to the user.",
                },
            },
            "required": ["question"],
        },
    ),
    "create_table": ToolDefinition(
        name="create_table",
        description="Create and define a new database table or schema (DDL). Generates clean CREATE TABLE statement with appropriate types, primary keys, and constraints.",
        parameters={
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Name of the new table to create.",
                },
                "ddl_sql": {
                    "type": "string",
                    "description": "The complete, dialect-accurate CREATE TABLE SQL statement generated by the model.",
                    "default": "",
                },
                "columns_description": {
                    "type": "string",
                    "description": "Optional notes on column purpose or constraints.",
                    "default": "",
                },
            },
            "required": ["table_name"],
        },
    ),
    "alter_table": ToolDefinition(
        name="alter_table",
        description="Alter or modify an existing database table schema (DDL, e.g. adding columns, modifying columns, dropping columns).",
        parameters={
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Name of the existing table to alter.",
                },
                "ddl_sql": {
                    "type": "string",
                    "description": "The complete, dialect-accurate ALTER TABLE SQL statement generated by the model.",
                    "default": "",
                },
            },
            "required": ["table_name"],
        },
    ),
    "create_index": ToolDefinition(
        name="create_index",
        description="Create an index on one or more columns of an existing table to improve query performance (DDL).",
        parameters={
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Name of the existing table to index.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of column names to index.",
                    "default": [],
                },
                "index_name": {
                    "type": "string",
                    "description": "Optional custom name for the index.",
                    "default": "",
                },
                "unique": {
                    "type": "boolean",
                    "description": "Whether the index enforces uniqueness.",
                    "default": False,
                },
                "ddl_sql": {
                    "type": "string",
                    "description": "The complete, dialect-accurate CREATE INDEX SQL statement generated by the model.",
                    "default": "",
                },
            },
            "required": ["table_name"],
        },
    ),
    "ddl_guidance": ToolDefinition(
        name="ddl_guidance",
        description="Alias for create_table: provide schema design guidance and suggested CREATE TABLE DDL SQL.",
        parameters={
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Name of the table being requested or designed.",
                },
                "columns_description": {
                    "type": "string",
                    "description": "Description of columns or purpose requested by user.",
                    "default": "",
                },
            },
            "required": ["table_name"],
        },
    ),
}


def get_available_tools() -> list[ToolDefinition]:
    """Return all configured database tool definitions."""
    return list(DATABASE_TOOLS.values())


def get_tool(name: str) -> ToolDefinition:
    """Look up tool by name."""
    if name not in DATABASE_TOOLS:
        raise KeyError(f"Tool '{name}' is not registered.")
    return DATABASE_TOOLS[name]
