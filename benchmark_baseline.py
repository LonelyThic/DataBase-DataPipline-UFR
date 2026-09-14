import json
import os
import subprocess
import time
from pathlib import Path

DB = "topipeline_benchmark"
USER = "postgres"
PSQL = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"

ROOT = Path(__file__).resolve().parent
SQL_DIR = ROOT / "SQL files"

RECIPES = [
    ("01_table3.sql", "public.table3"),
    ("02_table4.sql", "public.table4"),
    ("03_table1.sql", "public.table1"),
    ("04_table5.sql", "public.table5"),
    ("05_table6.sql", "public.table6"),
    ("06_table2.sql", "public.table2"),
]


def run_psql(sql: str) -> str:
    env = os.environ.copy()

    # Use the same password mechanism as the application's local
    # PostgreSQL command execution.
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    password = (
        os.getenv("PG_KAGGLE_CHALLENGE_PASS")
        or os.getenv("POSTGRES_PASSWORD")
        or os.getenv("PGPASSWORD")
    )

    if password is not None:
        env["PGPASSWORD"] = password

    result = subprocess.run(
        [
            PSQL,
            "-h", "localhost",
            "-U", USER,
            "-d", DB,
            "-X",
            "-qAt",
            "-v", "ON_ERROR_STOP=1",
            "-f", "-",
        ],
        input=sql,
        text=True,
        capture_output=True,
        env=env,
    )

    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError("psql failed")

    return result.stdout.strip()


def prepare():
    # Empty all destination tables and reset their managed counters.
    sql = """
TRUNCATE TABLE
    public.table1,
    public.table2,
    public.table3,
    public.table4,
    public.table5,
    public.table6
RESTART IDENTITY CASCADE;

UPDATE public.pgdm_table_row_counts
SET row_count = 0,
    updated_at = clock_timestamp(),
    last_reconciled_at = clock_timestamp();
"""
    run_psql(sql)


def load_recipes():
    statements = []

    for filename, destination in RECIPES:
        path = SQL_DIR / filename
        sql = path.read_text(encoding="utf-8").strip()

        # The official recipes use {{source}} as their source-table
        # placeholder.
        sql = sql.replace(
            "{{source}}",
            "public.raw_data",
        )

        statements.append((filename, destination, sql))

    return statements


def managed_statement(sql: str, destination: str) -> str:
    schema, table = destination.split(".", 1)

    return f"""
DO $pgdm_benchmark$
DECLARE
    affected bigint;
BEGIN
    {sql}

    GET DIAGNOSTICS affected = ROW_COUNT;

    UPDATE public.pgdm_table_row_counts
    SET row_count = row_count + affected,
        updated_at = clock_timestamp()
    WHERE schema_name = {schema!r}
      AND table_name = {table!r};

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Missing row counter for %.%',
            {schema!r},
            {table!r};
    END IF;
END
$pgdm_benchmark$;
""".strip()


def build_transaction(recipes):
    pieces = []

    pieces.append("""
BEGIN ISOLATION LEVEL REPEATABLE READ;

SET LOCAL client_min_messages = warning;

LOCK TABLE
    public.table3,
    public.table4,
    public.table1,
    public.table5,
    public.table6,
    public.table2
IN SHARE ROW EXCLUSIVE MODE;
""".strip())

    for filename, destination, sql in recipes:
        pieces.append(
            managed_statement(sql, destination)
        )

    pieces.append("""
COMMIT;
""".strip())

    return "\n\n".join(pieces)


def counts():
    output = run_psql("""
SELECT json_object_agg(table_name, row_count ORDER BY table_name)
FROM public.pgdm_table_row_counts
WHERE schema_name = 'public'
  AND table_name IN (
      'table1','table2','table3',
      'table4','table5','table6'
  );
""")

    return json.loads(output)


def main():
    print("Preparing clean baseline...")
    prepare()

    recipes = load_recipes()

    print("Recipes:")
    for filename, destination, _ in recipes:
        print(f"  {filename} -> {destination}")

    transaction_sql = build_transaction(recipes)

    print()
    print("Running baseline...")
    print()

    started = time.perf_counter()

    run_psql(transaction_sql)

    elapsed = time.perf_counter() - started

    result = {
        "database": DB,
        "source_rows": int(
            run_psql("SELECT count(*) FROM public.raw_data;")
        ),
        "elapsed_seconds": round(elapsed, 6),
        "destination_counts": counts(),
    }

    print()
    print("BASELINE RESULT")
    print("================")
    print(json.dumps(result, indent=2))

    output = ROOT / "baseline_result.json"
    output.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()


