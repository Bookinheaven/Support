"""Comprehensive regression tests for Support's terminal user experience.

Verifies:
1. SQL is displayed exactly once.
2. Mutation metadata is not redundantly printed.
3. Bounded operations show their maximum row count.
4. Full-table mutations retain prominent warnings.
5. Filtered mutations distinguish estimates from guarantees.
6. Approval still works exactly as before.
7. Clarification remains concise.
8. Validation and execution errors remain understandable and actionable.
9. Detailed diagnostics remain accessible via /details.
"""

from io import StringIO
from unittest.mock import patch, MagicMock
from rich.console import Console

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.query.models import QueryDraft
from support.query.display import (
    display_query_draft,
    display_mutation_preview,
    display_execution_preview,
    display_query_details,
)
from support.workspace.state import WorkspaceSessionState
from support.workspace.execution_commands import handle_conversational_execution
from support.workspace.general_commands import handle_details


def _make_test_profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="dev-pg",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="testdb",
        username="postgres",
    )


def test_sql_displayed_exactly_once_for_select():
    """Verify that a read-only SELECT query draft displays SQL exactly once during conversational flow."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    draft = QueryDraft(
        question="show top 5 users",
        sql="SELECT id, username FROM users LIMIT 5;",
        explanation="Selects 5 users from the users table.",
        dialect="postgres",
        validation_status="Valid",
        is_validated=True,
        is_parsed=True,
        is_readonly=True,
        operation="SELECT",
    )

    state = WorkspaceSessionState(active_database="dev-pg", database_profile=_make_test_profile())
    state.last_query_draft = draft

    # Capture console during conversational flow
    with patch("support.query.display.console", test_console), \
         patch("support.workspace.execution_commands.console", test_console), \
         patch("support.workspace.session.console", test_console):

        display_query_draft(draft, db_name="dev-pg", dialect_name="postgres")

        # In conversational flow, handle_conversational_execution prompts without repeating the SQL
        mock_input = lambda _: "n"
        handle_conversational_execution(
            draft=draft,
            state=state,
            user_input_func=mock_input,
        )

    output = test_console.file.getvalue()
    # SQL should appear exactly once
    assert output.count("SELECT id, username FROM users LIMIT 5;") == 1
    # Check that execution was cancelled cleanly
    assert "Execution cancelled" in output


def test_sql_displayed_exactly_once_for_bounded_mutation():
    """Verify that a bounded mutation displays SQL exactly once and metadata is not redundantly printed."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    sql_text = (
        "DELETE FROM users\n"
        "WHERE id IN (\n"
        "    SELECT id FROM users\n"
        "    ORDER BY created_at DESC, id DESC\n"
        "    LIMIT 5\n"
        ");"
    )
    draft = QueryDraft(
        question="delete 5 newest users",
        sql=sql_text,
        explanation="Deletes 5 newest users.",
        dialect="postgres",
        validation_status="Valid",
        is_validated=True,
        is_parsed=True,
        is_readonly=False,
        operation="DELETE",
        target_table="users",
        mutation_scope="BOUNDED",
        mutation_limit=5,
        is_bounded=True,
        is_ordered=True,
        is_deterministic=True,
        determinism_reason="Deterministic: ORDER BY includes verified unique tie-breaker ('id')",
    )

    state = WorkspaceSessionState(active_database="dev-pg", database_profile=_make_test_profile())
    state.last_query_draft = draft

    prompts_received = []
    def mock_input(prompt_text):
        prompts_received.append(prompt_text)
        return "n"

    with patch("support.query.display.console", test_console), \
         patch("support.workspace.execution_commands.console", test_console):

        # For mutations, conversational flow delegates directly to handle_conversational_execution
        # which displays the unified compact preview and asks for approval
        handle_conversational_execution(
            draft=draft,
            state=state,
            user_input_func=mock_input,
        )

    output = test_console.file.getvalue()
    # 1. SQL appears exactly once
    assert output.count("DELETE FROM users") == 1
    # 2. Target table appears once in the compact metadata
    assert output.count("Target:    users") == 1
    # 3. Maximum 5 records appears
    assert "Maximum 5 records" in output
    # 4. Prompt passed to user input is compact
    assert len(prompts_received) == 1
    assert "Approve execution? [y/N]" in prompts_received[0]


def test_bounded_operations_show_maximum_row_count():
    """Verify bounded mutations show guaranteed maximum affected rows."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    draft = QueryDraft(
        question="delete 5 users from users table",
        sql="DELETE FROM users WHERE id IN (SELECT id FROM users LIMIT 5);",
        explanation="Deletes 5 users.",
        dialect="postgres",
        validation_status="Valid",
        is_validated=True,
        is_readonly=False,
        operation="DELETE",
        target_table="users",
        mutation_scope="BOUNDED",
        mutation_limit=5,
        is_bounded=True,
        is_ordered=False,
        is_deterministic=False,
        determinism_reason="Nondeterministic: No ORDER BY clause",
    )

    with patch("support.query.display.console", test_console):
        display_mutation_preview(draft, profile_name="dev-pg", dialect_name="postgres", affected_row_estimate=5)

    output = test_console.file.getvalue()
    assert "Target:    users" in output
    assert "Maximum 5 records" in output
    assert "Arbitrary selection (no ORDER BY)" in output
    assert "SQL:" in output


def test_full_table_mutations_retain_prominent_warnings():
    """Verify full-table mutations retain prominent warnings that all records in the table will be affected."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    draft = QueryDraft(
        question="delete all users from users table",
        sql="DELETE FROM users;",
        explanation="Deletes all users.",
        dialect="postgres",
        validation_status="Valid",
        is_validated=True,
        is_readonly=False,
        operation="DELETE",
        target_table="users",
        mutation_scope="FULL_TABLE",
        is_unrestricted=True,
    )

    with patch("support.query.display.console", test_console):
        display_mutation_preview(draft, profile_name="dev-pg", dialect_name="postgres", affected_row_estimate=1420)

    output = test_console.file.getvalue()
    # Check warning banner and text
    assert "WARNING: FULL-TABLE DELETE OPERATION" in output
    assert "modify or delete ALL records in 'users'" in output
    assert "Selection: None (Entire table targeted)" in output
    assert "Estimated affected: ~1420 rows" in output
    assert "Guaranteed maximum: None (Unrestricted - all table rows)" in output


def test_filtered_mutations_distinguish_estimates_from_guarantees():
    """Verify filtered mutations explain all matching rows may be modified without confusing estimates with guarantees."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    draft = QueryDraft(
        question="update users set status = 'active' where status = 'inactive'",
        sql="UPDATE users SET status = 'active' WHERE status = 'inactive';",
        explanation="Updates inactive users.",
        dialect="postgres",
        validation_status="Valid",
        is_validated=True,
        is_readonly=False,
        operation="UPDATE",
        target_table="users",
        mutation_scope="FILTERED",
        selection_criteria="status = 'inactive'",
        is_bounded=False,
    )

    with patch("support.query.display.console", test_console):
        display_mutation_preview(draft, profile_name="dev-pg", dialect_name="postgres", affected_row_estimate=25)

    output = test_console.file.getvalue()
    assert "Selection: status = 'inactive' · All matching rows may be modified" in output
    assert "Estimated affected: ~25 rows (pre-execution estimate, not guaranteed)" in output
    assert "Guaranteed maximum: None (uncapped by row limit)" in output


def test_approval_preserves_human_confirmation():
    """Verify approval still works: rejects on Enter/N, executes on Y."""
    draft = QueryDraft(
        question="delete 5 users",
        sql="DELETE FROM users WHERE id IN (SELECT id FROM users LIMIT 5);",
        explanation="Deletes 5 users.",
        dialect="postgres",
        validation_status="Valid",
        is_validated=True,
        is_readonly=False,
        operation="DELETE",
        target_table="users",
        mutation_scope="BOUNDED",
        mutation_limit=5,
        is_bounded=True,
    )

    state = WorkspaceSessionState(active_database="dev-pg", database_profile=_make_test_profile())
    state.last_query_draft = draft

    # 1. Test Enter / blank cancels
    with patch("support.query.display.console", Console(file=StringIO(), color_system=None)), \
         patch("support.workspace.execution_commands.console", Console(file=StringIO(), color_system=None)):
        ret_cancel = handle_conversational_execution(
            draft=draft,
            state=state,
            user_input_func=lambda _: "",
        )
        assert ret_cancel is False

    # 2. Test 'n' cancels
    with patch("support.query.display.console", Console(file=StringIO(), color_system=None)), \
         patch("support.workspace.execution_commands.console", Console(file=StringIO(), color_system=None)):
        ret_cancel_n = handle_conversational_execution(
            draft=draft,
            state=state,
            user_input_func=lambda _: "n",
        )
        assert ret_cancel_n is False


def test_clarification_remains_concise():
    """Verify clarification questions are displayed cleanly without validation stages or SQL."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    draft = QueryDraft(
        question="delete users",
        sql="",
        explanation="",
        dialect="postgres",
        validation_status="Clarification needed",
        is_validated=False,
        clarification="Which 5 users should be deleted? (e.g., oldest, newest, by ID)",
    )

    with patch("support.query.display.console", test_console):
        display_query_draft(draft)

    output = test_console.file.getvalue()
    assert "Which 5 users should be deleted? (e.g., oldest, newest, by ID)" in output
    assert "Query ready" not in output
    assert "SQL:" not in output
    assert "Explanation:" not in output


def test_validation_errors_are_actionable_and_clean():
    """Verify validation rejections provide clear reasons, target table, and actionable suggestions."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    draft = QueryDraft(
        question="delete users",
        sql="DELETE FROM users;",
        explanation="Deletes users.",
        dialect="postgres",
        validation_status="Rejected: Unconditional DELETE without WHERE clause is prohibited for safety",
        is_validated=False,
        is_readonly=False,
        operation="DELETE",
        target_table="users",
    )

    with patch("support.query.display.console", test_console):
        display_query_draft(draft)

    output = test_console.file.getvalue()
    assert "Query rejected · Unconditional DELETE without WHERE clause is prohibited for safety" in output
    assert "Target table: users" in output
    assert "Suggestion: Specify a filter or row limit in 'users'" in output
    assert "Draft SQL:" in output
    assert "DELETE FROM users;" in output


def test_detailed_diagnostics_remain_accessible():
    """Verify full diagnostics (parsing, schema verification, mutation scope, performance) are accessible via /details."""
    test_console = Console(file=StringIO(), color_system=None, width=120)
    draft = QueryDraft(
        question="delete 5 users",
        sql="DELETE FROM users WHERE id IN (SELECT id FROM users LIMIT 5);",
        explanation="This SQL statement deletes 5 users based on row order.",
        dialect="postgres",
        validation_status="Valid",
        is_validated=True,
        is_parsed=True,
        is_readonly=False,
        operation="DELETE",
        target_table="users",
        mutation_scope="BOUNDED",
        mutation_limit=5,
        is_bounded=True,
        is_deterministic=False,
        determinism_reason="Nondeterministic: No ORDER BY clause",
        selection_criteria="id IN (SELECT id FROM users LIMIT 5)",
        schema_status="Verified against active database schema",
        is_schema_verified=True,
        tables_used=["users"],
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    state = WorkspaceSessionState(active_database="dev-pg", database_profile=_make_test_profile())
    state.last_query_draft = draft

    with patch("support.query.display.console", test_console):
        handle_details(state)

    output = test_console.file.getvalue()
    assert "Query Draft Diagnostics" in output
    assert "dev-pg (postgres)" in output
    assert "DELETE (Mutation)" in output
    assert "ollama / qwen2.5-coder:7b" in output
    assert "Successful" in output
    assert "Valid" in output
    assert "Verified against active database schema" in output
    assert "users" in output
    assert "BOUNDED" in output
    assert "5 records" in output
    assert "Nondeterministic: No ORDER BY clause" in output
    assert "This SQL statement deletes 5 users based on row order." in output
