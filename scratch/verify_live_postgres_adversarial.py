"""Live adversarial verification against active PostgreSQL database with rollback,
and SQLite dialect-specific execution tests.
"""
import sys
import sqlite3
import sqlglot
from sqlalchemy import text
from support.db.service import DatabaseService
from support.db.connection import DatabaseConnectionFactory
from support.query.schema_context import inspect_database_schema
from support.query.validator import analyze_mutation_scope
from support.query.models import QueryDraft
from support.query.display import display_mutation_preview
from rich.console import Console

def test_sqlite_adversarial():
    print("\n==========================================")
    print("RUNNING ADVERSARIAL VERIFICATION ON SQLITE")
    print("==========================================")
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, status TEXT, created_at TEXT);
        CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO users VALUES (1, 'a@a.com', 'inactive', '2026-01-01');
        INSERT INTO users VALUES (2, 'a@a.com', 'inactive', '2026-01-01');
        INSERT INTO users VALUES (3, 'b@b.com', 'inactive', '2026-01-02');
        INSERT INTO users VALUES (4, 'b@b.com', 'active', '2026-01-02');
        INSERT INTO users VALUES (5, 'c@c.com', 'inactive', '2026-01-03');
        INSERT INTO users VALUES (6, 'c@c.com', 'inactive', '2026-01-03');
        INSERT INTO students VALUES (1, 'Alice'), (2, 'Bob');
    """)
    conn.commit()

    import sqlglot
    # Case A on SQLite
    sql_a = "WITH selected AS (SELECT id FROM students LIMIT 1) DELETE FROM users WHERE status = 'inactive';"
    scope_a = analyze_mutation_scope(sqlglot.parse_one(sql_a, read="sqlite"))
    print(f"Case A (Unrelated CTE) -> is_bounded: {scope_a.is_bounded}, scope_type: {scope_a.scope_type}")
    assert not scope_a.is_bounded
    assert scope_a.scope_type == "FILTERED"

    # SQLite execution of Case A inside savepoint
    cur.execute("SAVEPOINT sp_a")
    before_a = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    cur.execute(sql_a)
    after_a = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    affected_a = before_a - after_a
    cur.execute("ROLLBACK TO sp_a")
    print(f"Case A executed on SQLite: affected {affected_a} rows (all 5 inactive users, ignoring CTE limit)")
    assert affected_a == 5

    # Case B on SQLite: email non-unique
    sql_b = "DELETE FROM users WHERE email IN (SELECT email FROM users ORDER BY email LIMIT 1);"
    scope_b = analyze_mutation_scope(sqlglot.parse_one(sql_b, read="sqlite"))
    print(f"Case B (Non-unique column) -> is_bounded: {scope_b.is_bounded}, scope_type: {scope_b.scope_type}")
    assert not scope_b.is_bounded

    # SQLite execution of Case B: limit 1 subquery deletes 2 rows because 'a@a.com' appears twice!
    cur.execute("SAVEPOINT sp_b")
    before_b = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    cur.execute(sql_b)
    after_b = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    affected_b = before_b - after_b
    cur.execute("ROLLBACK TO sp_b")
    print(f"Case B executed on SQLite: affected {affected_b} rows (limit was 1, but affected 2 rows due to non-unique email)")
    assert affected_b == 2

    # Case C on SQLite: PK
    sql_c = "DELETE FROM users WHERE id IN (SELECT id FROM users LIMIT 2);"
    from support.schema.models import DatabaseSchema, TableSchema, ColumnSchema
    sqlite_schema = DatabaseSchema(
        tables={
            "users": TableSchema(
                name="users",
                columns=[
                    ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
                    ColumnSchema(name="email", data_type="TEXT"),
                    ColumnSchema(name="status", data_type="TEXT"),
                ],
                primary_key=["id"],
            )
        }
    )
    scope_c = analyze_mutation_scope(sqlglot.parse_one(sql_c, read="sqlite"), schema=sqlite_schema)
    print(f"Case C (PK column) -> is_bounded: {scope_c.is_bounded}, limit: {scope_c.limit_count}")
    assert scope_c.is_bounded
    assert scope_c.limit_count == 2

    cur.execute("SAVEPOINT sp_c")
    before_c = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    cur.execute(sql_c)
    after_c = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    affected_c = before_c - after_c
    cur.execute("ROLLBACK TO sp_c")
    print(f"Case C executed on SQLite: affected {affected_c} rows (guaranteed max 2 rows)")
    assert affected_c == 2

    conn.close()
    print("SQLite adversarial execution tests PASSED successfully!")

def test_postgres_adversarial():
    print("\n==============================================")
    print("RUNNING ADVERSARIAL VERIFICATION ON POSTGRESQL")
    print("==============================================")
    dbs = DatabaseService()
    profile, password = dbs.manager.get_profile_with_password("athena")
    if not profile:
        print("Profile 'athena' not found, skipping live Postgres test.")
        return

    engine = DatabaseConnectionFactory.create_engine(profile, password=password, timeout=10, readonly=False)

    tbl_users = "_test_adv_users"
    tbl_orgs = "_test_adv_orgs"
    tbl_students = "_test_adv_students"

    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl_users} CASCADE;"))
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl_orgs} CASCADE;"))
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl_students} CASCADE;"))

            conn.execute(text(f"""
                CREATE TABLE {tbl_orgs} (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL
                );
            """))
            conn.execute(text(f"""
                CREATE TABLE {tbl_students} (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL
                );
            """))
            conn.execute(text(f"""
                CREATE TABLE {tbl_users} (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(100) NOT NULL,
                    org_id INT REFERENCES {tbl_orgs}(id),
                    status VARCHAR(50) NOT NULL,
                    created_at TIMESTAMP NOT NULL
                );
            """))

            # Insert sample rows
            for i in range(1, 6):
                conn.execute(text(f"INSERT INTO {tbl_orgs} (id, name) VALUES ({i}, 'Org {i}');"))
                conn.execute(text(f"INSERT INTO {tbl_students} (id, name) VALUES ({i}, 'Student {i}');"))

            # Insert users with duplicate emails and identical timestamps to test non-unique behaviors
            users_data = [
                (1, 'dup@example.com', 1, 'inactive', '2026-01-01 10:00:00'),
                (2, 'dup@example.com', 1, 'inactive', '2026-01-01 10:00:00'),
                (3, 'dup@example.com', 2, 'inactive', '2026-01-01 10:00:00'),
                (4, 'single@example.com', 2, 'inactive', '2026-01-02 10:00:00'),
                (5, 'user5@example.com', 3, 'inactive', '2026-01-03 10:00:00'),
                (6, 'user6@example.com', 3, 'active', '2026-01-03 10:00:00'),
                (7, 'user7@example.com', 4, 'inactive', '2026-01-04 10:00:00'),
            ]
            for uid, em, org, st, cat in users_data:
                conn.execute(text(f"""
                    INSERT INTO {tbl_users} (id, email, org_id, status, created_at)
                    VALUES ({uid}, '{em}', {org}, '{st}', '{cat}');
                """))

    # Inspect schema live
    schema = inspect_database_schema(profile, db_service=dbs)
    print(f"Inspected live schema: {len(schema.tables)} tables.")
    user_tbl = schema.get_table(tbl_users)
    print(f"Table {tbl_users}: PK = {user_tbl.primary_key if user_tbl else None}")

    cases = [
        (
            "Adversarial Case A (Unrelated CTE)",
            f"""WITH selected AS (
                SELECT id FROM {tbl_students} LIMIT 2
            )
            DELETE FROM {tbl_users} WHERE status = 'inactive';""",
            {"is_bounded": False, "scope_type": "FILTERED"}
        ),
        (
            "Adversarial Case B (Non-unique target column)",
            f"""DELETE FROM {tbl_users}
            WHERE email IN (
                SELECT email FROM {tbl_users}
                ORDER BY email LIMIT 1
            );""",
            {"is_bounded": False, "scope_type": "FILTERED"}
        ),
        (
            "Adversarial Case C (Target primary key)",
            f"""DELETE FROM {tbl_users}
            WHERE id IN (
                SELECT id FROM {tbl_users} LIMIT 2
            );""",
            {"is_bounded": True, "limit_count": 2, "is_deterministic": False}
        ),
        (
            "Adversarial Case D (Bounded but nondeterministic)",
            f"""DELETE FROM {tbl_users}
            WHERE id IN (
                SELECT id FROM {tbl_users} LIMIT 3
            );""",
            {"is_bounded": True, "limit_count": 3, "is_deterministic": False}
        ),
        (
            "Adversarial Case E (Ordered but not uniquely ordered)",
            f"""DELETE FROM {tbl_users}
            WHERE id IN (
                SELECT id FROM {tbl_users}
                ORDER BY created_at
                LIMIT 3
            );""",
            {"is_bounded": True, "limit_count": 3, "is_ordered": True, "is_deterministic": False}
        ),
        (
            "Adversarial Case F (Ordered with unique tie-breaker)",
            f"""DELETE FROM {tbl_users}
            WHERE id IN (
                SELECT id FROM {tbl_users}
                ORDER BY created_at ASC, id ASC
                LIMIT 3
            );""",
            {"is_bounded": True, "limit_count": 3, "is_ordered": True, "is_deterministic": True}
        ),
        (
            "Prompt Case (Subquery on related table organizations)",
            f"""DELETE FROM {tbl_users}
            WHERE org_id IN (
                SELECT id FROM {tbl_orgs}
                ORDER BY id LIMIT 2
            );""",
            {"is_bounded": False, "scope_type": "FILTERED"}
        ),
    ]

    console = Console()

    for name, sql, expected in cases:
        print(f"\n--- Testing: {name} ---")
        scope = analyze_mutation_scope(sqlglot.parse_one(sql, read="postgres"), schema=schema)
        print(f"Result -> is_bounded: {scope.is_bounded}, limit: {scope.limit_count}, "
              f"is_ordered: {scope.is_ordered}, is_deterministic: {scope.is_deterministic}")
        print(f"Reason: {scope.determinism_reason}")

        for k, v in expected.items():
            actual_val = getattr(scope, k)
            assert actual_val == v, f"Expected {k}={v}, got {actual_val} in {name}"

        # Test preview rendering
        draft = QueryDraft(
            question="adversarial delete",
            sql=sql,
            explanation="adversarial delete test",
            dialect="postgres",
            validation_status="Valid",
            is_validated=True,
            operation="DELETE",
            is_readonly=False,
            mutation_scope=scope.scope_type,
            mutation_limit=scope.limit_count,
            is_bounded=scope.is_bounded,
            is_deterministic=scope.is_deterministic,
            determinism_reason=scope.determinism_reason,
            target_table=scope.target_table,
            selection_criteria=scope.selection_criteria,
        )
        print("Rendering preview:")
        display_mutation_preview(
            draft,
            profile_name="athena",
            dialect_name="PostgreSQL",
            affected_row_estimate=2 if scope.is_bounded else 6,
        )

        # Execute inside a transaction and ROLLBACK to observe live affected rows
        with engine.connect() as conn:
            with conn.begin() as trans:
                res = conn.execute(text(sql))
                print(f"Executed on PostgreSQL! Live affected rows: {res.rowcount}")
                if name.startswith("Adversarial Case B"):
                    # email was limit 1, but 'dup@example.com' has 3 rows!
                    print(f"Notice: Limit was 1 subquery row, but affected {res.rowcount} actual rows due to non-unique key!")
                    assert res.rowcount == 3
                elif scope.is_bounded:
                    assert res.rowcount <= scope.limit_count
                trans.rollback()

    # Cleanup test tables
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl_users} CASCADE;"))
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl_orgs} CASCADE;"))
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl_students} CASCADE;"))

    print("\nALL POSTGRESQL ADVERSARIAL CASES PASSED WITH 100% INTEGRITY AND ROLLBACK!")

if __name__ == "__main__":
    test_sqlite_adversarial()
    test_postgres_adversarial()
