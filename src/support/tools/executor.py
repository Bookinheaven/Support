"""Execution coordinator for application database tools."""

import re
from typing import Any, Callable, Optional

from support.cli.display import console
from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.history.store import QueryHistoryStore
from support.llm.models import GenerationSettings
from support.llm.service import LLMService
from support.mutation.display import display_mutation_preview, display_mutation_result
from support.mutation.executor import DataModificationExecutor
from support.mutation.models import MutationType, WhereCondition
from support.mutation.planner import RawMutationProposal, build_change_plan, compute_plan_hash, resolve_mutation_target_table
from support.mutation.sample_data import hash_password, populate_sample_and_derived_values
from support.query.display import display_query_draft
from support.query.generator import QueryGenerator
from support.query.inspection import build_inspection_plan, execute_multi_table_inspection
from support.query.schema_context import inspect_database_schema
from support.tools.models import ToolCall, ToolResult
from support.workspace.general_commands import handle_diagram, handle_schema
from support.workspace.state import WorkspaceSessionState


def execute_database_tool(
    call: ToolCall,
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    llm_service: Optional[LLMService] = None,
    user_input_func: Callable[[str], str] = input,
    executor: Optional[DataModificationExecutor] = None,
    store: Optional[QueryHistoryStore] = None,
    original_question: str = "",
) -> ToolResult:
    """Validate arguments and execute selected database tool with full security controls."""
    dbs = db_service or DatabaseService()
    llms = llm_service or LLMService()
    exec_inst = executor or DataModificationExecutor()
    hist_store = store or QueryHistoryStore()

    profile = state.database_profile
    if not profile:
        console.print("[bold yellow]No active database selected.[/bold yellow]")
        console.print("[dim]Use /db to select an active database before executing database operations.[/dim]\n")
        return ToolResult(tool=call.tool, success=False, error="No active database selected.")

    # 1. Discover schema if not cached
    if state.cached_schema is None and profile.dialect != Dialect.MONGODB:
        with console.status(f"[cyan]Discovering schema for '{state.active_database}'...[/cyan]", spinner="dots"):
            try:
                state.cached_schema = inspect_database_schema(profile, db_service=dbs)
            except Exception as e:
                console.print(f"[bold red]Schema discovery failed:[/bold red] {e}\n")
                return ToolResult(tool=call.tool, success=False, error=str(e))

    schema = state.cached_schema

    # -------------------------------------------------------------
    # Tool: list_tables
    # -------------------------------------------------------------
    if call.tool == "list_tables":
        handle_schema(state, db_service=dbs)
        state.add_message("assistant", f"Displayed discovered schema tables for {state.active_database}.")
        return ToolResult(tool=call.tool, success=True, display_text="Displayed schema tables.")

    # -------------------------------------------------------------
    # Tool: generate_diagram
    # -------------------------------------------------------------
    elif call.tool == "generate_diagram":
        tables = call.arguments.get("tables")
        with console.status("[cyan]Generating entity-relationship diagram...[/cyan]", spinner="dots"):
            handle_diagram(state, db_service=dbs, tables=tables)
        state.add_message("assistant", f"Generated ER diagram for {state.active_database}.")
        return ToolResult(tool=call.tool, success=True, display_text="Generated ER diagram.")

    # -------------------------------------------------------------
    # Tool: inspect_tables
    # -------------------------------------------------------------
    elif call.tool == "inspect_tables":
        if not schema or not schema.tables:
            console.print(f"[yellow]No accessible tables discovered in '{state.active_database}'.[/yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="No tables discovered.")

        target_tables = call.arguments.get("target_tables")
        row_limit = call.arguments.get("row_limit", 1)
        plan = build_inspection_plan(
            profile=profile,
            schema=schema,
            target_tables=target_tables,
            row_limit=row_limit,
            user_input_func=user_input_func,
        )
        if plan is not None:
            state.last_inspection_plan = plan
            with console.status("[cyan]Inspecting and sampling tables...[/cyan]", spinner="dots"):
                execute_multi_table_inspection(
                    plan=plan,
                    profile=profile,
                    state=state,
                    db_service=dbs,
                    user_input_func=user_input_func,
                    original_question=original_question,
                )
        return ToolResult(tool=call.tool, success=True, display_text="Multi-table inspection completed.")

    # -------------------------------------------------------------
    # Tool: query_database
    # -------------------------------------------------------------
    elif call.tool == "query_database":
        query_text = call.arguments.get("query", original_question)
        generator = QueryGenerator(db_service=dbs, llm_service=llms)
        draft = generator.generate_draft(query_text, state)

        display_query_draft(
            draft,
            db_name=state.active_database,
            dialect_name=profile.dialect.value if profile else None,
        )

        state.last_query_draft = draft
        state.add_message(
            "assistant",
            f"Generated query draft: {draft.sql}" if draft.sql else draft.explanation,
            metadata={"draft_sql": draft.sql, "validation_status": draft.validation_status},
        )
        return ToolResult(tool=call.tool, success=True, output=draft)

    # -------------------------------------------------------------
    # Tool: create_record
    # -------------------------------------------------------------
    elif call.tool == "create_record":
        if profile.dialect == Dialect.MONGODB:
            console.print()
            console.print("[bold yellow]MongoDB write operations are not supported.[/bold yellow]")
            console.print("  [dim]Data modification is supported for relational databases: PostgreSQL, MySQL, SQLite, and SQL Server.[/dim]\n")
            state.add_message("assistant", "MongoDB write operations are not supported.", metadata={"rejected": True})
            return ToolResult(tool=call.tool, success=False, error="MongoDB write operations are not supported.")

        if not schema or not schema.tables:
            console.print(f"[yellow]No accessible tables discovered in '{state.active_database}'.[/yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="No tables discovered.")

        raw_tbl = call.arguments.get("table_name", "")
        # Resolve table name against live schema
        resolved_tbl, tbl_schema, err = resolve_mutation_target_table(raw_tbl or original_question, schema)
        if err:
            console.print(f"[bold red]{err}[/bold red]\n")
            state.add_message("assistant", err, metadata={"rejected": True})
            return ToolResult(tool=call.tool, success=False, error=err)

        if not resolved_tbl or not tbl_schema:
            console.print(f"[bold red]Could not identify a valid target table from '{raw_tbl}'.[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error="Target table not found.")

        allow_sample = bool(call.arguments.get("allow_sample_data", False))
        initial_values = dict(call.arguments.get("values", {}))

        # Check for explicit password provided to hash before storing
        if "password" in initial_values and "password_hash" not in initial_values:
            raw_pw = initial_values.pop("password")
            initial_values["password_hash"] = hash_password(str(raw_pw))

        # Populate sample and derived values
        resolved_values, missing_required = populate_sample_and_derived_values(
            table=tbl_schema,
            schema=schema,
            initial_values=initial_values,
            profile=profile,
            db_service=dbs,
            allow_sample_data=allow_sample,
        )

        # If required values are still missing, ask the user conversationally
        if missing_required:
            from support.mutation.models import PendingMutation

            state.pending_mutation = PendingMutation(
                operation=MutationType.INSERT,
                table_name=tbl_schema.name,
                values=resolved_values,
                missing_fields=missing_required,
                allow_sample_data=allow_sample,
                question=original_question,
            )
            missing_str = ", ".join(missing_required)
            prompt_msg = f"To add a record to '{tbl_schema.name}', what {missing_str} should I use?"
            console.print(f"\n[cyan]{prompt_msg}[/cyan]\n")
            state.add_message("assistant", prompt_msg, metadata={"pending_fields": missing_required})
            return ToolResult(tool=call.tool, success=True, display_text=prompt_msg)

        # Compile and validate change plan
        proposal = RawMutationProposal(
            operation=MutationType.INSERT,
            table_name=tbl_schema.name,
            values=resolved_values,
            conditions={},
            inferred_reasons={},
        )
        plan = build_change_plan(proposal, schema, profile)
        if not plan.is_valid:
            console.print(f"\n[bold red]Change Plan Rejected:[/bold red] {plan.validation_error}\n")
            return ToolResult(tool=call.tool, success=False, error=plan.validation_error, change_plan=plan)

        # Display preview and prompt for explicit user approval
        display_mutation_preview(plan, profile)
        try:
            raw_ans = user_input_func("Apply this change? [y/N]: ").strip().lower()
            confirmed = raw_ans in ("y", "yes")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Change cancelled. No modifications were made.[/dim]\n")
            return ToolResult(tool=call.tool, success=False, error="User cancelled.")

        if not confirmed:
            console.print(f"[dim]Change cancelled. No modifications were made to '{profile.name}'.[/dim]\n")
            return ToolResult(tool=call.tool, success=False, error="User declined approval.")

        # Execute inside transaction
        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        result = exec_inst.execute_mutation(profile, password, plan)
        display_mutation_result(result)

        # Save to query history
        try:
            hist_store.record_query(
                database=profile.name,
                dialect=profile.dialect.value,
                natural_query=original_question,
                sql=plan.sql,
                status=result.status,
                row_count=result.affected_rows,
                duration_ms=result.duration_ms,
                error_message=result.error_message,
                metadata={"plan_id": plan.plan_id, "operation": plan.operation.value, "parameters": plan.parameters},
            )
        except Exception:
            pass

        state.pending_mutation = None
        return ToolResult(tool=call.tool, success=result.is_success, output=result, change_plan=plan)

    # -------------------------------------------------------------
    # Tool: update_records
    # -------------------------------------------------------------
    elif call.tool == "update_records":
        if profile.dialect == Dialect.MONGODB:
            console.print("[bold yellow]MongoDB write operations are not supported.[/bold yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="MongoDB write operations are not supported.")

        raw_tbl = call.arguments.get("table_name", "")
        resolved_tbl, tbl_schema, err = resolve_mutation_target_table(raw_tbl or original_question, schema)
        if err or not tbl_schema:
            msg = err or f"Target table '{raw_tbl}' not found."
            console.print(f"[bold red]{msg}[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error=msg)

        conditions = call.arguments.get("conditions", {})
        values = call.arguments.get("values", {})
        if not conditions:
            msg = "Rejected: Unconditional UPDATE without a WHERE clause is prohibited."
            console.print(f"[bold red]{msg}[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error=msg)

        # Inspect target records
        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        from support.mutation.models import WhereCondition
        tmp_conds = [WhereCondition(column=k, operator="=", value=v) for k, v in conditions.items()]
        preview_records, matched_count = exec_inst.inspect_target_records(profile, password, tbl_schema.name, tmp_conds)

        if matched_count == 0:
            console.print(f"\n[yellow]No records found in '{tbl_schema.name}' matching the conditions. No changes made.[/yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="No matching records.")

        proposal = RawMutationProposal(
            operation=MutationType.UPDATE,
            table_name=tbl_schema.name,
            values=values,
            conditions=conditions,
            inferred_reasons={},
        )
        plan = build_change_plan(proposal, schema, profile, preview_records=preview_records, matched_count=matched_count)
        if not plan.is_valid:
            console.print(f"\n[bold red]Change Plan Rejected:[/bold red] {plan.validation_error}\n")
            return ToolResult(tool=call.tool, success=False, error=plan.validation_error)

        display_mutation_preview(plan, profile)
        try:
            confirmed = user_input_func("Apply this change? [y/N]: ").strip().lower() in ("y", "yes")
        except (KeyboardInterrupt, EOFError):
            return ToolResult(tool=call.tool, success=False, error="User cancelled.")

        if not confirmed:
            console.print(f"[dim]Change cancelled. No modifications were made.[/dim]\n")
            return ToolResult(tool=call.tool, success=False, error="User declined.")

        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        result = exec_inst.execute_mutation(profile, password, plan)
        display_mutation_result(result)
        return ToolResult(tool=call.tool, success=result.is_success, output=result)

    # -------------------------------------------------------------
    # Tool: delete_records
    # -------------------------------------------------------------
    elif call.tool == "delete_records":
        if profile.dialect == Dialect.MONGODB:
            console.print("[bold yellow]MongoDB write operations are not supported.[/bold yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="MongoDB write operations are not supported.")

        raw_tbl = call.arguments.get("table_name", "")
        resolved_tbl, tbl_schema, err = resolve_mutation_target_table(raw_tbl or original_question, schema)
        if err or not tbl_schema:
            msg = err or f"Target table '{raw_tbl}' not found."
            console.print(f"[bold red]{msg}[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error=msg)

        conditions = call.arguments.get("conditions", {})
        if not conditions:
            msg = "Rejected: Unconditional DELETE without a WHERE clause is prohibited."
            console.print(f"[bold red]{msg}[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error=msg)

        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        from support.mutation.models import WhereCondition
        tmp_conds = [WhereCondition(column=k, operator="=", value=v) for k, v in conditions.items()]
        preview_records, matched_count = exec_inst.inspect_target_records(profile, password, tbl_schema.name, tmp_conds)

        if matched_count == 0:
            console.print(f"\n[yellow]No records found in '{tbl_schema.name}' matching the conditions. No changes made.[/yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="No matching records.")

        proposal = RawMutationProposal(
            operation=MutationType.DELETE,
            table_name=tbl_schema.name,
            values={},
            conditions=conditions,
            inferred_reasons={},
        )
        plan = build_change_plan(proposal, schema, profile, preview_records=preview_records, matched_count=matched_count)
        if not plan.is_valid:
            console.print(f"\n[bold red]Change Plan Rejected:[/bold red] {plan.validation_error}\n")
            return ToolResult(tool=call.tool, success=False, error=plan.validation_error)

        display_mutation_preview(plan, profile)
        try:
            if plan.matched_record_count > 1:
                confirmed = user_input_func(f"Type 'DELETE' to confirm removing {plan.matched_record_count} records: ").strip() == "DELETE"
            else:
                confirmed = user_input_func("Apply this change? [y/N]: ").strip().lower() in ("y", "yes")
        except (KeyboardInterrupt, EOFError):
            return ToolResult(tool=call.tool, success=False, error="User cancelled.")

        if not confirmed:
            console.print(f"[dim]Change cancelled. No modifications were made.[/dim]\n")
            return ToolResult(tool=call.tool, success=False, error="User declined.")

        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        result = exec_inst.execute_mutation(profile, password, plan)
        display_mutation_result(result)
        return ToolResult(tool=call.tool, success=result.is_success, output=result)

    # -------------------------------------------------------------
    # Tool: explain_error / ask_clarification
    # -------------------------------------------------------------
    elif call.tool == "explain_error":
        err_msg = call.arguments.get("error_message", "")
        console.print(f"\n[bold yellow]Error Explanation:[/bold yellow] {err_msg}\n")
        return ToolResult(tool=call.tool, success=True, display_text=err_msg)

    elif call.tool == "ask_clarification":
        q = call.arguments.get("question", "")
        console.print(f"\n[cyan]{q}[/cyan]\n")
        return ToolResult(tool=call.tool, success=True, display_text=q)

    elif call.tool in ("create_table", "ddl_guidance"):
        tbl_name = call.arguments.get("table_name", "example_table")
        ddl_sql = call.arguments.get("ddl_sql", "").strip()
        dialect_enum = profile.dialect if profile else Dialect.POSTGRES

        if profile and profile.dialect == Dialect.MONGODB:
            console.print("\n[bold yellow]MongoDB collection/table creation via SQL DDL is not supported.[/bold yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="MongoDB DDL not supported.")

        if not ddl_sql and llms:
            ddl_sql = _generate_ddl_with_llm(
                user_request=original_question or f"Create table {tbl_name}",
                dialect=dialect_enum,
                llm_service=llms,
                state=state,
            )

        if not ddl_sql:
            cols_desc = call.arguments.get("columns_description", "").strip()
            if cols_desc:
                raw_parts = [p.strip() for p in re.split(r"[,;]|(?:\band\b)", cols_desc) if p.strip()]
                col_defs = []
                for p in raw_parts:
                    p_clean = re.sub(r"^(?:with|and)\s+", "", p, flags=re.IGNORECASE).strip()
                    first_word = p_clean.split()[0] if p_clean else ""
                    if first_word:
                        col_name = re.sub(r"[^a-zA-Z0-9_]", "", first_word)
                        if not col_name:
                            continue
                        if col_name.lower() == "id":
                            if dialect_enum == Dialect.POSTGRES:
                                col_defs.append(f"    {col_name} SERIAL PRIMARY KEY")
                            elif dialect_enum == Dialect.SQLITE:
                                col_defs.append(f"    {col_name} INTEGER PRIMARY KEY AUTOINCREMENT")
                            elif dialect_enum == Dialect.MYSQL:
                                col_defs.append(f"    {col_name} INT AUTO_INCREMENT PRIMARY KEY")
                            elif dialect_enum == Dialect.MSSQL:
                                col_defs.append(f"    {col_name} INT IDENTITY(1,1) PRIMARY KEY")
                            else:
                                col_defs.append(f"    {col_name} INT PRIMARY KEY")
                        elif any(k in col_name.lower() for k in ("time", "date", "_at")):
                            col_defs.append(f"    {col_name} TIMESTAMP")
                        elif col_name.lower().startswith(("is_", "has_")):
                            col_defs.append(f"    {col_name} BOOLEAN")
                        elif any(k in col_name.lower() for k in ("count", "amount", "num", "price", "total")):
                            col_defs.append(f"    {col_name} NUMERIC")
                        else:
                            col_defs.append(f"    {col_name} VARCHAR(255)")
                if col_defs:
                    ddl_sql = f"CREATE TABLE {tbl_name} (\n" + ",\n".join(col_defs) + "\n);"

        if not ddl_sql:
            q = f"How would you like to define the columns for table '{tbl_name}'?"
            console.print(f"\n[cyan]{q}[/cyan]\n")
            return ToolResult(tool=call.tool, success=True, display_text=q)

        console.print()
        console.print(f"[bold cyan]Proposed Table Definition for '{tbl_name}' ({dialect_enum.value}):[/bold cyan]")
        from rich.syntax import Syntax
        syntax = Syntax(ddl_sql, "sql", theme="monokai", line_numbers=False)
        console.print(syntax)
        console.print()

        target_db_name = profile.name if profile else state.active_database or "database"
        try:
            raw_ans = user_input_func(f"Execute this table creation on '{target_db_name}'? [y/N]: ").strip().lower()
            confirmed = raw_ans in ("y", "yes")
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[dim]Table creation cancelled. No modifications were made to '{target_db_name}'.[/dim]\n")
            return ToolResult(tool=call.tool, success=False, error="User cancelled.")

        if not confirmed:
            console.print(f"[dim]Table creation cancelled. No modifications were made to '{target_db_name}'.[/dim]\n")
            state.add_message("assistant", f"Table creation cancelled for '{tbl_name}'.")
            return ToolResult(tool=call.tool, success=True, output=ddl_sql, display_text="Table creation cancelled by user.")

        if not profile:
            console.print("[bold red]Cannot execute table creation: No active database profile.[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error="No active database profile.")

        # User approved: execute DDL
        from sqlalchemy import text
        from support.db.connection import DatabaseConnectionFactory

        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        try:
            with console.status(f"[cyan]Creating table '{tbl_name}' in '{profile.name}'...[/cyan]", spinner="dots"):
                engine = DatabaseConnectionFactory.create_engine(
                    profile,
                    password=password,
                    timeout=10,
                    readonly=False,
                )
                with engine.connect() as conn:
                    with conn.begin():
                        conn.execute(text(ddl_sql))
                engine.dispose()

            # Refresh schema cache so newly created table is available immediately
            state.cached_schema = inspect_database_schema(profile, db_service=dbs)

            console.print(f"\n[bold green]Table '{tbl_name}' created successfully in '{profile.name}'.[/bold green]\n")
            state.add_message(
                "assistant",
                f"Table '{tbl_name}' created successfully in '{profile.name}'.",
                metadata={"ddl_sql": ddl_sql},
            )
            return ToolResult(tool=call.tool, success=True, output=ddl_sql, display_text=f"Table '{tbl_name}' created successfully.")
        except Exception as e:
            console.print(f"\n[bold red]Failed to create table '{tbl_name}':[/bold red] {e}\n")
            state.add_message("assistant", f"Failed to create table '{tbl_name}': {e}", metadata={"error": str(e)})
            return ToolResult(tool=call.tool, success=False, error=str(e))

    elif call.tool == "alter_table":
        tbl_name = call.arguments.get("table_name", "")
        action = call.arguments.get("action", "add_column")
        col_name = call.arguments.get("column_name", "")
        col_type = call.arguments.get("column_type", "VARCHAR(255)")
        ddl_sql = call.arguments.get("ddl_sql", "")

        dialect_enum = profile.dialect if profile else Dialect.POSTGRES

        if profile and profile.dialect == Dialect.MONGODB:
            console.print("\n[bold yellow]MongoDB collection modification via SQL DDL is not supported.[/bold yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="MongoDB DDL not supported.")

        if not tbl_name and schema:
            tbl_name, _, _ = resolve_mutation_target_table(original_question, schema)

        if not tbl_name:
            err = "Could not identify target table to alter."
            console.print(f"\n[bold red]{err}[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error=err)

        if not ddl_sql and llms:
            ddl_sql = _generate_ddl_with_llm(
                user_request=original_question or f"Alter table {tbl_name}",
                dialect=dialect_enum,
                llm_service=llms,
                state=state,
            )

        if not ddl_sql:
            q = f"How would you like to alter table '{tbl_name}'?"
            console.print(f"\n[cyan]{q}[/cyan]\n")
            return ToolResult(tool=call.tool, success=True, display_text=q)

        console.print()
        console.print(f"[bold cyan]Proposed Schema Modification for '{tbl_name}' ({dialect_enum.value}):[/bold cyan]")
        from rich.syntax import Syntax
        syntax = Syntax(ddl_sql, "sql", theme="monokai", line_numbers=False)
        console.print(syntax)
        console.print()

        target_db_name = profile.name if profile else state.active_database or "database"
        try:
            raw_ans = user_input_func(f"Execute this schema modification on '{target_db_name}'? [y/N]: ").strip().lower()
            confirmed = raw_ans in ("y", "yes")
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[dim]Schema modification cancelled. No modifications were made to '{target_db_name}'.[/dim]\n")
            return ToolResult(tool=call.tool, success=False, error="User cancelled.")

        if not confirmed:
            console.print(f"[dim]Schema modification cancelled. No modifications were made to '{target_db_name}'.[/dim]\n")
            state.add_message("assistant", f"Schema modification cancelled for '{tbl_name}'.")
            return ToolResult(tool=call.tool, success=True, output=ddl_sql, display_text="Schema modification cancelled by user.")

        if not profile:
            console.print("[bold red]Cannot execute schema modification: No active database profile.[/bold red]\n")
            return ToolResult(tool=call.tool, success=False, error="No active database profile.")

        from sqlalchemy import text
        from support.db.connection import DatabaseConnectionFactory

        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        try:
            with console.status(f"[cyan]Modifying table '{tbl_name}' in '{profile.name}'...[/cyan]", spinner="dots"):
                engine = DatabaseConnectionFactory.create_engine(
                    profile,
                    password=password,
                    timeout=10,
                    readonly=False,
                )
                with engine.connect() as conn:
                    with conn.begin():
                        conn.execute(text(ddl_sql))
                engine.dispose()

            state.cached_schema = inspect_database_schema(profile, db_service=dbs)

            console.print(f"\n[bold green]Table '{tbl_name}' modified successfully in '{profile.name}'.[/bold green]\n")
            state.add_message(
                "assistant",
                f"Table '{tbl_name}' modified successfully in '{profile.name}'.",
                metadata={"ddl_sql": ddl_sql},
            )
            return ToolResult(tool=call.tool, success=True, output=ddl_sql, display_text=f"Table '{tbl_name}' modified successfully.")
        except Exception as e:
            console.print(f"\n[bold red]Failed to modify table '{tbl_name}':[/bold red] {e}\n")
            state.add_message("assistant", f"Failed to modify table '{tbl_name}': {e}", metadata={"error": str(e)})
            return ToolResult(tool=call.tool, success=False, error=str(e))

    # -------------------------------------------------------------
    # Tool: create_index
    # -------------------------------------------------------------
    elif call.tool == "create_index":
        if profile.dialect == Dialect.MONGODB:
            console.print("\n[bold yellow]MongoDB index creation through this workflow is not supported.[/bold yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="MongoDB index creation is not supported.")

        tbl_name = call.arguments.get("table_name", "")
        cols = call.arguments.get("columns", [])
        index_name = call.arguments.get("index_name", "")
        is_unique = bool(call.arguments.get("unique", False))
        explicit_sql = call.arguments.get("ddl_sql", "").strip()

        if not tbl_name:
            console.print("[yellow]Could not identify target table for index.[/yellow]\n")
            return ToolResult(tool=call.tool, success=False, error="Target table not specified.")

        if not explicit_sql and cols:
            idx_cols = ", ".join(cols)
            idx_name = index_name or f"idx_{tbl_name}_{'_'.join(cols)}"
            unique_str = "UNIQUE " if is_unique else ""
            explicit_sql = f"CREATE {unique_str}INDEX {idx_name} ON {tbl_name} ({idx_cols});"
        elif not explicit_sql and llms:
            dialect_enum = profile.dialect if profile else Dialect.POSTGRES
            with console.status(f"[cyan]Generating index for '{tbl_name}'...[/cyan]", spinner="dots"):
                explicit_sql = _generate_ddl_with_llm(
                    user_request=original_question or f"Create index on {tbl_name}",
                    dialect=dialect_enum,
                    llm_service=llms,
                    state=state,
                )

        from rich.syntax import Syntax

        console.print()
        console.print(f"[bold cyan]Proposed Index Definition on '{tbl_name}' ({profile.dialect.value}):[/bold cyan]")
        console.print(Syntax(explicit_sql, "sql", theme="monokai", line_numbers=False))
        console.print()

        try:
            confirmed = user_input_func(f"Execute this index creation on '{profile.name}'? [y/N]: ").strip().lower() in ("y", "yes")
        except (KeyboardInterrupt, EOFError):
            confirmed = False

        if not confirmed:
            console.print(f"\n[yellow]Index creation cancelled. No modifications were made to '{profile.name}'.[/yellow]\n")
            return ToolResult(tool=call.tool, success=True, output=explicit_sql, display_text="Index creation cancelled by user.")

        from sqlalchemy import text
        from support.db.connection import DatabaseConnectionFactory

        password = None
        try:
            _, password = dbs.manager.get_profile_with_password(profile.name)
        except Exception:
            pass

        try:
            with console.status(f"[cyan]Creating index on '{tbl_name}' in '{profile.name}'...[/cyan]", spinner="dots"):
                engine = DatabaseConnectionFactory.create_engine(
                    profile,
                    password=password,
                    timeout=10,
                    readonly=False,
                )
                with engine.connect() as conn:
                    with conn.begin():
                        conn.execute(text(explicit_sql))
                engine.dispose()

            state.cached_schema = inspect_database_schema(profile, db_service=dbs)
            console.print(f"\n[bold green]Index created successfully on '{tbl_name}' in '{profile.name}'.[/bold green]\n")
            state.add_message("assistant", f"Index created successfully on '{tbl_name}'.", metadata={"ddl_sql": explicit_sql})
            return ToolResult(tool=call.tool, success=True, output=explicit_sql, display_text=f"Index created successfully on '{tbl_name}'.")
        except Exception as e:
            console.print(f"\n[bold red]Failed to create index on '{tbl_name}':[/bold red] {e}\n")
            state.add_message("assistant", f"Failed to create index on '{tbl_name}': {e}", metadata={"error": str(e)})
            return ToolResult(tool=call.tool, success=False, error=str(e))

    return ToolResult(tool=call.tool, success=False, error=f"Unknown tool '{call.tool}'.")


def _generate_ddl_with_llm(
    user_request: str,
    dialect: Dialect,
    llm_service: LLMService,
    state: Optional[WorkspaceSessionState] = None,
) -> str:
    """Prompt the LLM directly to understand the request and generate clean DDL."""
    try:
        target_provider = state.active_provider if state else None
        target_model = state.active_model if state else None

        prompt = (
            f"You are a database architect. Write a single, clean SQL DDL statement "
            f"for {dialect.value.upper()} to satisfy the user request.\n"
            f"User request: {user_request}\n\n"
            f"Rules:\n"
            f"- Tolerant of conversational phrasing and typos.\n"
            f"- Propose appropriate data types, constraints, and primary keys for {dialect.value.upper()}.\n"
            f"- Output ONLY the SQL statement inside a ```sql code block. No conversational text."
        )
        with console.status(f"[cyan]Generating {dialect.value} DDL statement...[/cyan]", spinner="dots"):
            resp = llm_service.generate(
                prompt=prompt,
                provider=target_provider,
                model=target_model,
                settings=GenerationSettings(temperature=0.0, timeout=15.0),
            )
        raw = resp.text.strip()
        m_sql = re.search(r"```(?:sql)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        sql_candidate = m_sql.group(1).strip() if m_sql else raw.strip()
        if any(sql_candidate.upper().startswith(k) for k in ("CREATE", "ALTER", "DROP")):
            if not sql_candidate.endswith(";"):
                sql_candidate += ";"
            return sql_candidate
    except Exception:
        pass
    return ""



def resume_pending_mutation(
    user_response: str,
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    user_input_func: Callable[[str], str] = input,
    executor: Optional[DataModificationExecutor] = None,
    store: Optional[QueryHistoryStore] = None,
) -> bool:
    """Resume a pending conversational mutation by populating newly supplied user values."""
    pending = state.pending_mutation
    if not pending or not state.database_profile:
        return False

    clean_resp = user_response.strip()
    norm = clean_resp.lower()

    # Check for cancellation
    if re.search(r"\b(cancel|abort|stop|quit|nevermind|no)\b", norm) and len(clean_resp.split()) <= 3:
        state.pending_mutation = None
        console.print(f"\n[dim]Change cancelled. No modifications were made to '{state.database_profile.name}'.[/dim]\n")
        state.add_message("assistant", "Change cancelled. No modifications were made.")
        return True

    schema = state.cached_schema
    tbl_schema = schema.get_table(pending.table_name) if schema else None
    if not tbl_schema:
        state.pending_mutation = None
        return False

    # Extract user-supplied values from response
    # Can be comma-separated list or keyword arguments (username=..., email=...)
    new_vals: dict[str, Any] = {}
    for m in re.finditer(r"\b([a-zA-Z0-9_]+)\s*[:=]\s*(?:'([^']*)'|\"([^\"]*)\"|([^\s,]+))\b", clean_resp):
        k, v1, v2, v3 = m.groups()
        val = v1 or v2 or v3
        new_vals[k.lower()] = val

    # If comma-separated values matching remaining missing fields:
    # e.g. "john_doe, john@example.com, John Doe"
    if not new_vals and "," in clean_resp:
        parts = [p.strip() for p in clean_resp.split(",")]
        for idx, part in enumerate(parts):
            if idx < len(pending.missing_fields):
                target_field = pending.missing_fields[idx]
                new_vals[target_field] = part

    # Check for password or email in plain text
    m_email = re.search(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b", clean_resp)
    if m_email and "email" in pending.missing_fields and "email" not in new_vals:
        new_vals["email"] = m_email.group(1)

    # Update pending values
    pending.values.update(new_vals)

    # Check for raw password to hash
    if "password" in pending.values and "password_hash" not in pending.values:
        raw_pw = pending.values.pop("password")
        pending.values["password_hash"] = hash_password(str(raw_pw))

    # Re-evaluate sample and derived values
    resolved_values, missing = populate_sample_and_derived_values(
        table=tbl_schema,
        schema=schema,
        initial_values=pending.values,
        profile=state.database_profile,
        db_service=db_service,
        allow_sample_data=pending.allow_sample_data,
    )
    pending.values = resolved_values
    pending.missing_fields = missing

    if missing:
        missing_str = ", ".join(missing)
        prompt_msg = f"Still need value(s) for '{pending.table_name}': {missing_str}."
        console.print(f"\n[cyan]{prompt_msg}[/cyan]\n")
        state.add_message("assistant", prompt_msg)
        return True

    # All values are ready! Delegate to create_record tool call
    call = ToolCall(
        tool="create_record",
        arguments={
            "table_name": pending.table_name,
            "values": pending.values,
            "allow_sample_data": pending.allow_sample_data,
        },
    )
    execute_database_tool(
        call=call,
        state=state,
        db_service=db_service,
        user_input_func=user_input_func,
        executor=executor,
        store=store,
        original_question=pending.question,
    )
    return True
