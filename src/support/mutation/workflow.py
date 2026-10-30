"""Conversational execution workflow for controlled CRUD data modifications."""

from typing import Callable, Optional

from support.cli.display import console
from support.core.dialect import Dialect
from support.db.service import DatabaseService
from support.history.store import QueryHistoryStore
from support.llm.service import LLMService
from support.mutation.display import display_mutation_preview, display_mutation_result
from support.mutation.executor import DataModificationExecutor
from support.mutation.models import ChangePlan, MutationResult, MutationType
from support.mutation.planner import build_change_plan, compute_plan_hash, propose_mutation_plan
from support.query.schema_context import inspect_database_schema
from support.workspace.state import WorkspaceSessionState


def handle_mutation_workflow(
    question: str,
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    llm_service: Optional[LLMService] = None,
    user_input_func: Callable[[str], str] = input,
    executor: Optional[DataModificationExecutor] = None,
    store: Optional[QueryHistoryStore] = None,
) -> bool:
    """Execute conversational change plan proposal, preview, explicit approval, and transaction execution."""
    dbs = db_service or DatabaseService()
    exec_inst = executor or DataModificationExecutor()
    hist_store = store or QueryHistoryStore()

    # 1. Require active database
    if not state.active_database or not state.database_profile:
        console.print("[bold yellow]No active database selected. Use /db to choose an active database before modifying data.[/bold yellow]\n")
        return False

    profile = state.database_profile

    # 2. Block MongoDB from relational write pipeline
    if profile.dialect == Dialect.MONGODB:
        console.print()
        console.print("[bold yellow]MongoDB write operations are not supported.[/bold yellow]")
        console.print("  [dim]Data modification is currently supported for relational databases: PostgreSQL, MySQL, SQLite, and SQL Server.[/dim]\n")
        state.add_message("assistant", "MongoDB write operations are not supported.", metadata={"rejected": True})
        return False

    # 3. Discover schema if not cached
    if state.cached_schema is None:
        with console.status(f"[cyan]Discovering schema for '{state.active_database}'...[/cyan]", spinner="dots"):
            try:
                state.cached_schema = inspect_database_schema(profile, db_service=dbs)
            except Exception as e:
                console.print(f"[bold red]Schema discovery failed:[/bold red] {e}\n")
                return False

    schema = state.cached_schema
    if not schema or not schema.tables:
        console.print(f"[yellow]No accessible tables discovered in '{state.active_database}'.[/yellow]\n")
        return False

    # 4. Propose structured mutation plan
    proposal = propose_mutation_plan(
        question=question,
        schema=schema,
        profile=profile,
        llm_service=llm_service,
        target_provider=state.active_provider,
        target_model=state.active_model,
    )

    if not proposal or not proposal.table_name:
        console.print("[bold red]Could not identify the target table or operation for this request.[/bold red]")
        console.print("[dim]Please specify the table name and values (e.g. 'Add a user with username alex and email alex@example.com').[/dim]\n")
        return False

    # 5. Handle missing required fields by prompting user
    if proposal.missing_fields:
        for mf in proposal.missing_fields:
            try:
                val = user_input_func(f"Please enter value for '{mf}': ").strip()
                if val:
                    proposal.values[mf] = val
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Operation cancelled.[/dim]\n")
                return False

    # 6. Fetch database password
    password: Optional[str] = None
    try:
        _, password = dbs.manager.get_profile_with_password(profile.name)
    except Exception:
        pass

    # 7. Pre-execution record resolution for UPDATE and DELETE
    preview_records = []
    matched_count = 0
    if proposal.operation in (MutationType.UPDATE, MutationType.DELETE) and proposal.conditions:
        from support.mutation.models import WhereCondition
        tmp_conds = [WhereCondition(column=k, operator="=", value=v) for k, v in proposal.conditions.items()]
        preview_records, matched_count = exec_inst.inspect_target_records(
            profile=profile,
            password=password,
            table_name=proposal.table_name,
            conditions=tmp_conds,
        )

        if matched_count == 0:
            console.print()
            cond_str = ", ".join(f"{k} = {v}" for k, v in proposal.conditions.items())
            console.print(f"[yellow]No records found in '{proposal.table_name}' matching ({cond_str}). No changes made.[/yellow]\n")
            return False

    # 8. Build validated, compiled ChangePlan
    plan = build_change_plan(
        proposal=proposal,
        schema=schema,
        profile=profile,
        preview_records=preview_records,
        matched_count=matched_count,
    )

    if not plan.is_valid:
        console.print()
        console.print(f"[bold red]Change Plan Rejected:[/bold red] {plan.validation_error}\n")
        return False

    # 9. Display change preview
    display_mutation_preview(plan, profile)

    # 10. Request explicit user confirmation
    try:
        if plan.operation == MutationType.DELETE and plan.matched_record_count > 1:
            # Stronger confirmation for multi-record delete
            raw_ans = user_input_func(
                f"Type 'DELETE' to confirm removing {plan.matched_record_count} records from '{plan.table_name}': "
            ).strip()
            confirmed = raw_ans == "DELETE"
        else:
            raw_ans = user_input_func("Apply this change? [y/N]: ").strip().lower()
            confirmed = raw_ans in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Change cancelled. No modifications were made.[/dim]\n")
        return False

    if not confirmed:
        console.print(f"[dim]Change cancelled. No modifications were made to '{profile.name}'.[/dim]\n")
        return False

    # 11. Pre-execution revalidation and plan hash immutability check
    if state.active_database != profile.name:
        console.print("[bold red]Active database changed since approval. Modification aborted for safety.[/bold red]\n")
        return False

    current_hash = compute_plan_hash(
        database=profile.name,
        dialect_val=profile.dialect.value,
        operation_val=plan.operation.value,
        table_name=plan.table_name,
        sql=plan.sql,
        params=plan.parameters,
        matched_count=plan.matched_record_count,
    )
    if current_hash != plan.plan_hash:
        console.print("[bold red]Change plan modified since approval. Modification aborted for safety.[/bold red]\n")
        return False

    # 12. Execute mutation inside transactional boundary
    with console.status(f"[cyan]Applying {plan.operation.value} to '{profile.name}'...[/cyan]", spinner="dots"):
        result: MutationResult = exec_inst.execute_mutation(
            profile=profile,
            password=password,
            plan=plan,
        )

    # 13. Display result
    display_mutation_result(result)

    # 14. Record in Query History without storing credentials or result rows
    sanitized_sql = plan.sql
    hist_store.add_entry(
        question=question,
        sql=sanitized_sql,
        database=profile.name,
        dialect=profile.dialect.value,
        explanation=f"{plan.operation.value} on {plan.table_name} ({result.affected_rows} record(s))",
        is_validated=True,
        validation_status="Validated Change Plan",
        is_schema_verified=True,
        schema_status=f"Table '{plan.table_name}' verified",
        execution_status=result.status,
        duration_ms=result.duration_ms,
        row_count=result.affected_rows,
        error=result.error_message,
        entry_type="mutation",
    )

    # 15. Record in workspace state chat history
    state.add_message(
        "assistant",
        f"Executed {plan.operation.value} on {plan.table_name}: {result.status}.",
        metadata={
            "intent": "data_mutation",
            "operation": plan.operation.value,
            "table": plan.table_name,
            "status": result.status,
            "affected_rows": result.affected_rows,
            "duration_ms": result.duration_ms,
        },
    )

    return result.is_success
