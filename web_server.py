"""
web_server.py — ERPNext MCP Web Interface
Run: uvicorn web_server:app --host 0.0.0.0 --port 8000 --reload
Open: http://localhost:8000
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pymysql
import ollama
import re
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host":     "172.28.157.72",
    "port":     3306,
    "user":     "root",
    "password": "root@123",
    "database": "_b9e0741af4689f11",
}

LLM_MODEL       = "qwen2.5-coder:14b"
OLLAMA_BASE_URL = "http://localhost:11434"

ALLOWED_TABLES = [
    "tabPurchase Invoice",
    "tabPurchase Invoice Item",
    "tabSales Invoice",
    "tabSales Invoice Item",
    "tabSupplier",
    "tabCustomer",
    "tabItem",
    "tabEmployee",
    "tabPayment Entry",
    "tabJournal Entry",
    "tabStock Entry",
    "tabStock Ledger Entry",
    "tabBin",
]

# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------

app = FastAPI(title="ERPNext MCP Chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
client = ollama.Client(host=OLLAMA_BASE_URL)

# ---------------------------------------------------------------------------
# DB HELPERS
# ---------------------------------------------------------------------------

def get_connection(retries=3):
    for attempt in range(retries):
        try:
            return pymysql.connect(
                **DB_CONFIG,
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10,
                read_timeout=30,
                write_timeout=30,
            )
        except Exception as e:
            print(f"  [DB attempt {attempt+1} failed: {e}]")
            time.sleep(2)
    raise Exception("Could not connect to MariaDB.")


def run_query(sql: str) -> tuple:
    """Returns (rows, error_string)."""
    if not sql.strip().upper().startswith("SELECT"):
        return [], "Only SELECT queries are allowed."
    quoted = re.findall(r'`([^`]+)`', sql)
    used = [q for q in quoted if q.lower().startswith('tab')]
    for t in used:
        if t not in ALLOWED_TABLES:
            return [], f"Table `{t}` not in allowed list."
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        conn.close()
        print(f"  [Rows]: {len(rows)}")
        return rows, ""
    except Exception as e:
        return [], f"SQL error: {e}"

# ---------------------------------------------------------------------------
# RESULT FORMATTERS
# Three routes — same as api2.py:
#   1. Aggregate (COUNT/SUM/etc, 1 row) → format directly, NEVER through LLM
#   2. Listing question → detailed formatter
#   3. Everything else → LLM summarizer (but STRICTLY no invented data)
# ---------------------------------------------------------------------------

SKIP_FIELDS = {
    "creation", "modified", "modified_by", "owner", "docstatus",
    "naming_series", "idx", "posting_time", "set_posting_time",
    "conversion_rate", "buying_price_list", "price_list_currency",
    "plc_conversion_rate", "base_total", "base_net_total",
    "base_taxes_and_charges_added", "base_total_taxes_and_charges",
    "base_grand_total", "base_rounding_adjustment", "base_rounded_total",
    "base_in_words", "tax_withholding_net_total", "base_tax_withholding_net_total",
    "taxes_and_charges_added", "apply_discount_on", "other_charges_calculation",
    "party_account_currency", "is_opening", "language", "supplier_group",
    "taxes_and_charges", "tally_guid", "gst_vehicle_type", "gst_category",
    "title", "payment_terms_template",
}

AGGREGATE_KEYS = {
    "count", "total", "sum", "avg", "average", "max", "min",
    "amount", "balance", "tax", "due", "payable", "receivable",
    "purchases", "sales", "payments", "received", "paid", "qty",
    "employees", "customers", "suppliers", "invoices", "stock",
    "overdue", "unpaid", "outstanding",
}

LISTING_KEYWORDS  = ["all", "list", "show", "give", "every", "details", "detail"]
SUPERLATIVE_KEYS  = ["most", "highest", "largest", "maximum", "max",
                     "least", "lowest", "smallest", "minimum", "min",
                     "best", "worst", "top", "bottom"]
LIMITING_KEYWORDS = ["oldest", "newest", "latest", "first", "last", "recent"]
SUMMARY_KEYWORDS  = ["business summary", "how is my business", "overview",
                     "dashboard", "how am i doing", "financial summary"]


def is_listing_question(q: str) -> bool:
    return any(k in q.lower() for k in LISTING_KEYWORDS)

def is_superlative_question(q: str) -> bool:
    return any(k in q.lower() for k in SUPERLATIVE_KEYS)

def is_limiting_question(q: str) -> bool:
    return any(k in q.lower() for k in LIMITING_KEYWORDS)


def is_aggregate_result(rows: list) -> bool:
    """True when result is a single-row aggregate (COUNT, SUM, etc.)"""
    if len(rows) != 1:
        return False
    keys = [k.lower() for k in rows[0].keys()]
    return len(keys) <= 2 and any(agg in k for k in keys for agg in AGGREGATE_KEYS)


def format_aggregate_directly(rows: list) -> str:
    """
    Return aggregate value as plain string.
    NEVER passes through LLM — eliminates hallucination entirely.
    """
    row = rows[0]
    parts = []
    all_null = True
    for k, v in row.items():
        if v is None:
            continue
        all_null = False
        if isinstance(v, float):
            parts.append(f"₹{v:,.2f}")
        else:
            parts.append(str(v))

    if all_null or not parts:
        return None  # signal: no data found, trigger dual-table retry

    return ", ".join(parts)


def format_listing(rows: list) -> str:
    if not rows:
        return "No results found."
    lines = [f"Found {len(rows)} result(s):\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"--- {i} ---")
        for k, v in row.items():
            if k in SKIP_FIELDS or v is None or v == "" or v == 0:
                continue
            lines.append(f"  {k}: ₹{v:,.2f}" if isinstance(v, float) else f"  {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def format_rows_plain(rows: list) -> str:
    if not rows:
        return "No results found."
    lines = []
    for i, row in enumerate(rows, 1):
        parts = []
        for k, v in row.items():
            if v is None or v == "":
                continue
            parts.append(f"{k}: ₹{v:,.2f}" if isinstance(v, float) else f"{k}: {v}")
        lines.append(f"{i}. " + "  |  ".join(parts))
    return f"({len(rows)} row{'s' if len(rows)>1 else ''}):\n" + "\n".join(lines)

# ---------------------------------------------------------------------------
# SCHEMA CACHE
# ---------------------------------------------------------------------------

_schema_cache: dict = {}
_schema_fetched_at: float = 0
SCHEMA_TTL = 300


def get_schema_cached() -> str:
    global _schema_cache, _schema_fetched_at
    now = time.time()
    if _schema_cache and (now - _schema_fetched_at) < SCHEMA_TTL:
        return "\n".join(_schema_cache.values())
    print("  [Schema: refreshing from DB]")
    try:
        conn = get_connection()
        cursor = conn.cursor()
        lines = {}
        for table in ALLOWED_TABLES:
            try:
                cursor.execute(f"SHOW COLUMNS FROM `{table}`")
                cols = cursor.fetchall()
                lines[table] = (
                    f"`{table}`: " +
                    ", ".join(f"{r['Field']}({r['Type']})" for r in cols)
                )
            except:
                pass
        conn.close()
        _schema_cache = lines
        _schema_fetched_at = now
        return "\n".join(lines.values())
    except Exception as e:
        return f"Schema fetch error: {e}"

# ---------------------------------------------------------------------------
# SQL GENERATOR
# ---------------------------------------------------------------------------

def strip_invented_date_filters(sql: str, question: str) -> str:
    """
    If the user did NOT mention a date/time period, remove any hardcoded
    date filters the model invented (e.g. posting_date <= '2025-06-05').
    This is the root cause of incomplete results for entity queries.
    """
    # Keywords that legitimately require a date filter
    date_intent_words = [
        "this month", "last month", "this year", "last year", "today",
        "yesterday", "this week", "last week", "this quarter",
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
        "2020", "2021", "2022", "2023", "2024", "2025", "2026",
        "date", "month", "year", "week", "period", "between", "since", "before", "after",
    ]

    q_lower = question.lower()
    user_wants_date_filter = any(w in q_lower for w in date_intent_words)

    if user_wants_date_filter:
        return sql  # user asked for date filtering — keep it

    # Detect hardcoded date strings like '2025-06-05' or "2024-01-01"
    hardcoded_date = re.search(r"['\"](\d{4}-\d{2}-\d{2})['\"]", sql)
    if hardcoded_date:
        print(f"  [WARNING: Model added invented date filter: {hardcoded_date.group(0)}]")
        # Remove the entire condition containing the hardcoded date
        # Handles: AND posting_date <= '2025-06-05' and similar
        sql = re.sub(
            r'\s+AND\s+`?\w+`?\s*(<=|>=|<|>|=|!=|BETWEEN\s+[^\s]+\s+AND\s+[^\s]+)\s*[\'\"]\d{4}-\d{2}-\d{2}[\'\"]',
            '',
            sql,
            flags=re.IGNORECASE,
        )
        # Also handle: AND '2025-06-05' >= posting_date
        sql = re.sub(
            r'\s+AND\s+[\'\"]\d{4}-\d{2}-\d{2}[\'\"](\s*(<=|>=|<|>|=)\s*`?\w+`?)',
            '',
            sql,
            flags=re.IGNORECASE,
        )
        print(f"  [Date filter stripped. Cleaned SQL]: {sql[:200]}")

    return sql


def fix_table_names(sql: str) -> str:
    replacements = {
        r'(?<!`)tabSales Invoice Item(?!`)':    '`tabSales Invoice Item`',
        r'(?<!`)tabPurchase Invoice Item(?!`)': '`tabPurchase Invoice Item`',
        r'(?<!`)tabSales Invoice(?!`)':         '`tabSales Invoice`',
        r'(?<!`)tabPurchase Invoice(?!`)':      '`tabPurchase Invoice`',
        r'(?<!`)tabPayment Entry(?!`)':         '`tabPayment Entry`',
        r'(?<!`)tabJournal Entry(?!`)':         '`tabJournal Entry`',
        r'(?<!`)tabStock Entry(?!`)':           '`tabStock Entry`',
        r'(?<!`)tabStock Ledger Entry(?!`)':    '`tabStock Ledger Entry`',
        r'(?<!`)tabSupplier(?!`)':              '`tabSupplier`',
        r'(?<!`)tabCustomer(?!`)':              '`tabCustomer`',
        r'(?<!`)tabItem(?!`)':                  '`tabItem`',
        r'(?<!`)tabEmployee(?!`)':              '`tabEmployee`',
        r'(?<!`)tabBin(?!`)':                   '`tabBin`',
    }
    for pattern, replacement in replacements.items():
        sql = re.sub(pattern, replacement, sql)
    return sql


def extract_sql(raw: str) -> str:
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    if raw.strip().startswith('{') or raw.strip().startswith('['):
        print("  [Rejected: JSON output]")
        return ""
    fenced = re.search(r'```(?:sql)?\s*(SELECT\b.+?)```', raw, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip().rstrip(';')
    blank = re.search(r'(SELECT\b.+?)(?=\n\s*\n)', raw, re.IGNORECASE | re.DOTALL)
    if blank:
        return blank.group(1).strip().rstrip(';')
    fallback = re.search(r'(SELECT\b.+)', raw, re.IGNORECASE | re.DOTALL)
    if fallback:
        return fallback.group(1).strip().rstrip(';')
    return ""


def generate_sql(question: str, retry_reason: str = "") -> str:
    schema     = get_schema_cached()
    table_list = "\n".join(f"  - `{t}`" for t in ALLOWED_TABLES)
    retry_block = (
        f"\nPREVIOUS ATTEMPT FAILED: {retry_reason}\n"
        f"Fix the issue. Output ONLY the corrected SQL.\n"
    ) if retry_reason else ""

    system = (
        "You are a MariaDB SQL generator for ERPNext. "
        "Output ONLY a single raw SELECT statement. "
        "No JSON. No explanations. No markdown. "
        "Start with SELECT. End with semicolon. Nothing else."
    )

    user = f"""STRICT RULES:
1.  Wrap ALL table and column names in backticks.
2.  Always filter `docstatus` = 1 on transactional tables.
3.  SUPPLIER queries → `tabPurchase Invoice`, filter: `supplier` LIKE '%name%'
4.  CUSTOMER queries → `tabSales Invoice`,  filter: `customer` LIKE '%name%'
5.  NEVER use tabSales Invoice for supplier questions.
6.  NEVER use tabPurchase Invoice for customer questions.
7.  Unpaid/overdue → `status` IN ('Unpaid','Overdue') AND `docstatus` = 1
    NEVER use outstanding_amount > 0 alone for unpaid filter.
8.  For outstanding/unpaid AMOUNTS use `outstanding_amount` column. NOT `grand_total`.
    `grand_total` = total billed. `outstanding_amount` = still owed.
9.  Tax → `total_taxes_and_charges` column. NOT taxes_and_charges.
10. most/highest → ORDER BY ... DESC LIMIT 1
11. least/lowest → ORDER BY ... ASC LIMIT 1
12. top N → ORDER BY ... DESC LIMIT N
13. oldest → ORDER BY `posting_date` ASC LIMIT 1
14. newest/latest → ORDER BY `posting_date` DESC LIMIT 1
15. NEVER use UNION or UNION ALL.
16. Name filters ALWAYS use LIKE '%name%'. NEVER use = for names.
17. Stock → `tabBin` actual_qty column.
18. Payments out → `tabPayment Entry` WHERE `payment_type`='Pay'
19. When "invoices" has no context → default to `tabSales Invoice`
20. Output ONLY one SQL query. Never write multiple alternatives.
21. NEVER add a date filter (posting_date, due_date, creation, etc.) unless the
    user explicitly mentions a date, month, year, or time period in their question.
    Questions like "how many invoices does X have" or "what does X owe" refer to
    ALL records, not records before or after any date. Adding a date filter to
    these questions is WRONG and will return incomplete results.
22. NEVER use hardcoded dates like '2025-06-05' or any specific date string.
    Only use CURDATE(), MONTH(), YEAR() functions when the user asks about
    "this month", "this year", "today" etc.
{retry_block}
Available tables:
{table_list}

Live schema (column names per table):
{schema}

EXAMPLES (learn the pattern exactly):
Q: how many overdue purchase invoices does Hettich India Private Limited have
A: SELECT COUNT(*) AS `overdue_count` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `supplier` LIKE '%Hettich India%' AND `status` = 'Overdue';
NOTE: No date filter — user did not ask for a specific time period. ALL records.

Q: how many overdue purchase invoices does fms traders have
A: SELECT COUNT(*) AS `overdue_count` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `supplier` LIKE '%fms traders%' AND `status` = 'Overdue';
NOTE: No date filter — user did not ask for a specific time period. ALL records.

Q: how many invoices does supplier xyz have
A: SELECT COUNT(*) AS `invoice_count` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `supplier` LIKE '%xyz%';
NOTE: No date filter. "how many invoices does X have" = ALL invoices ever, no date limit.

Q: what is the outstanding amount for Hettich India
A: SELECT SUM(`outstanding_amount`) AS `total_outstanding` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `supplier` LIKE '%Hettich India%' AND `outstanding_amount` > 0;
NOTE: No date filter. Outstanding amount = all unpaid invoices regardless of date.

Q: how much tax did Golden Twist pay
A: SELECT SUM(`total_taxes_and_charges`) AS `total_tax` FROM `tabSales Invoice` WHERE `docstatus`=1 AND `customer` LIKE '%Golden Twist%';

Q: which supplier do i owe the most money to
A: SELECT `supplier`, SUM(`outstanding_amount`) AS `total_due` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `outstanding_amount`>0 GROUP BY `supplier` ORDER BY `total_due` DESC LIMIT 1;

Q: how many unpaid purchase invoices
A: SELECT COUNT(*) AS `unpaid_count` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `status` IN ('Unpaid','Overdue');

Q: how many unpaid sales invoices
A: SELECT COUNT(*) AS `unpaid_count` FROM `tabSales Invoice` WHERE `docstatus`=1 AND `status` IN ('Unpaid','Overdue');

Q: total purchase amount this month
A: SELECT SUM(`grand_total`) AS `total` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND MONTH(`posting_date`)=MONTH(CURDATE()) AND YEAR(`posting_date`)=YEAR(CURDATE());

Q: top 5 customers by revenue
A: SELECT `customer`, SUM(`grand_total`) AS `revenue` FROM `tabSales Invoice` WHERE `docstatus`=1 GROUP BY `customer` ORDER BY `revenue` DESC LIMIT 5;

Q: list all active employees
A: SELECT `employee_name`, `designation`, `department` FROM `tabEmployee` WHERE `status`='Active' ORDER BY `employee_name`;

Q: total outstanding amount supplier xyz owes
A: SELECT SUM(`outstanding_amount`) AS `total_outstanding` FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `supplier` LIKE '%xyz%' AND `outstanding_amount`>0;

Q: how much did customer abc pay in total
A: SELECT SUM(`grand_total`) AS `total_billed` FROM `tabSales Invoice` WHERE `docstatus`=1 AND `customer` LIKE '%abc%';

Question: {question}
SQL:"""

    resp = client.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        options={"temperature": 0},
    )
    raw = resp["message"]["content"].strip()
    print(f"  [LLM raw]: {raw[:300]}")
    sql = extract_sql(raw)
    if not sql:
        return ""
    sql = fix_table_names(sql)
    sql = strip_invented_date_filters(sql, question)  # remove hallucinated dates

    # Strip LIMIT only for non-superlative, non-limiting questions
    if not is_superlative_question(question) and not is_limiting_question(question):
        sql = re.sub(r'\bLIMIT\s+\d+\b', '', sql, flags=re.IGNORECASE).strip()

    sql = re.sub(r'\s+', ' ', sql).strip().rstrip(';')
    print(f"  [Final SQL]: {sql}")
    return sql

# ---------------------------------------------------------------------------
# DUAL-TABLE RETRY — searches name in both Sales and Purchase tables
# ---------------------------------------------------------------------------

def dual_table_search(name: str) -> str:
    """
    When a query returns 0 rows, search the entity name in BOTH
    tabSales Invoice (customer) and tabPurchase Invoice (supplier).
    Returns a combined result or a clear not-found message.
    """
    found = []

    # As customer in Sales Invoice
    s_sql = f"""
        SELECT 'Customer (Sales Invoice)' AS `found_as`,
               COUNT(*) AS `invoice_count`,
               SUM(`grand_total`) AS `total_billed`,
               SUM(`outstanding_amount`) AS `total_outstanding`,
               SUM(`total_taxes_and_charges`) AS `total_tax`
        FROM `tabSales Invoice`
        WHERE `docstatus`=1 AND `customer` LIKE '%{name}%'
    """
    s_rows, _ = run_query(s_sql)
    if s_rows and s_rows[0].get("invoice_count", 0):
        r = s_rows[0]
        parts = [f"Found as Customer"]
        if r.get("invoice_count"):  parts.append(f"Invoices: {r['invoice_count']}")
        if r.get("total_billed"):   parts.append(f"Total Billed: ₹{r['total_billed']:,.2f}")
        if r.get("total_outstanding") and r["total_outstanding"] > 0:
            parts.append(f"Outstanding: ₹{r['total_outstanding']:,.2f}")
        if r.get("total_tax") and r["total_tax"] > 0:
            parts.append(f"Tax: ₹{r['total_tax']:,.2f}")
        found.append("  ".join(parts))

    # As supplier in Purchase Invoice
    p_sql = f"""
        SELECT 'Supplier (Purchase Invoice)' AS `found_as`,
               COUNT(*) AS `invoice_count`,
               SUM(`grand_total`) AS `total_billed`,
               SUM(`outstanding_amount`) AS `total_outstanding`,
               SUM(`total_taxes_and_charges`) AS `total_tax`
        FROM `tabPurchase Invoice`
        WHERE `docstatus`=1 AND `supplier` LIKE '%{name}%'
    """
    p_rows, _ = run_query(p_sql)
    if p_rows and p_rows[0].get("invoice_count", 0):
        r = p_rows[0]
        parts = [f"Found as Supplier"]
        if r.get("invoice_count"):  parts.append(f"Invoices: {r['invoice_count']}")
        if r.get("total_billed"):   parts.append(f"Total Purchased: ₹{r['total_billed']:,.2f}")
        if r.get("total_outstanding") and r["total_outstanding"] > 0:
            parts.append(f"Payable: ₹{r['total_outstanding']:,.2f}")
        if r.get("total_tax") and r["total_tax"] > 0:
            parts.append(f"Tax: ₹{r['total_tax']:,.2f}")
        found.append("  ".join(parts))

    if not found:
        return f"No records found for '{name}' in Sales or Purchase invoices."
    return f"Results for '{name}':\n" + "\n".join(found)


def extract_entity_name(question: str) -> str:
    """Extract likely entity name from question by removing common question words."""
    stop = (
        r'\b(how many|how much|what is|what|show me|show|list|give me|give|did|does|do|'
        r'is|are|pay|paid|owe|owes|have|has|tax|invoice|invoices|total|amount|'
        r'outstanding|overdue|unpaid|for|to|by|from|the|a|an|i|my|me|'
        r'supplier|customer|purchase|sales|all|any|which|who|where|when|'
        r'number|count|many|much|get)\b'
    )
    cleaned = re.sub(stop, ' ', question, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Remove short leftovers
    words = [w for w in cleaned.split() if len(w) > 2]
    return " ".join(words)

# ---------------------------------------------------------------------------
# BUSINESS SUMMARY TOOL
# ---------------------------------------------------------------------------

def tool_get_business_summary() -> str:
    queries = {
        "Sales this month":             "SELECT SUM(`grand_total`) AS val FROM `tabSales Invoice` WHERE `docstatus`=1 AND MONTH(`posting_date`)=MONTH(CURDATE()) AND YEAR(`posting_date`)=YEAR(CURDATE())",
        "Purchases this month":         "SELECT SUM(`grand_total`) AS val FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND MONTH(`posting_date`)=MONTH(CURDATE()) AND YEAR(`posting_date`)=YEAR(CURDATE())",
        "Sales this year":              "SELECT SUM(`grand_total`) AS val FROM `tabSales Invoice` WHERE `docstatus`=1 AND YEAR(`posting_date`)=YEAR(CURDATE())",
        "Purchases this year":          "SELECT SUM(`grand_total`) AS val FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND YEAR(`posting_date`)=YEAR(CURDATE())",
        "Unpaid sales invoices":        "SELECT COUNT(*) AS val FROM `tabSales Invoice` WHERE `docstatus`=1 AND `status` IN ('Unpaid','Overdue')",
        "Unpaid purchase invoices":     "SELECT COUNT(*) AS val FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `status` IN ('Unpaid','Overdue')",
        "Total receivable (customers)": "SELECT SUM(`outstanding_amount`) AS val FROM `tabSales Invoice` WHERE `docstatus`=1 AND `outstanding_amount`>0",
        "Total payable (suppliers)":    "SELECT SUM(`outstanding_amount`) AS val FROM `tabPurchase Invoice` WHERE `docstatus`=1 AND `outstanding_amount`>0",
        "Top customer by revenue":      "SELECT `customer` AS val FROM `tabSales Invoice` WHERE `docstatus`=1 GROUP BY `customer` ORDER BY SUM(`grand_total`) DESC LIMIT 1",
        "Top supplier by purchases":    "SELECT `supplier` AS val FROM `tabPurchase Invoice` WHERE `docstatus`=1 GROUP BY `supplier` ORDER BY SUM(`grand_total`) DESC LIMIT 1",
    }
    try:
        conn = get_connection()
        cursor = conn.cursor()
        lines = ["Business Summary\n" + "="*40]
        for label, q in queries.items():
            try:
                cursor.execute(q)
                row = cursor.fetchone()
                val = list(row.values())[0] if row else None
                if val is None:
                    lines.append(f"  {label}: No data")
                elif isinstance(val, float):
                    lines.append(f"  {label}: ₹{val:,.2f}")
                else:
                    lines.append(f"  {label}: {val}")
            except Exception as e:
                lines.append(f"  {label}: Error — {e}")
        conn.close()
        return "\n".join(lines)
    except Exception as e:
        return f"Connection error: {e}"

# ---------------------------------------------------------------------------
# MAIN ORCHESTRATOR
# Routing identical to api2.py:
#   Route 1 — listing question → detailed formatter
#   Route 2 — aggregate result → format DIRECTLY, never through LLM
#   Route 3 — everything else → LLM summarizer (data only, no invention)
# ---------------------------------------------------------------------------

def answer_question(question: str) -> str:
    q = question.strip()

    # Shortcut: business summary
    if any(k in q.lower() for k in SUMMARY_KEYWORDS):
        return tool_get_business_summary()

    # Generate SQL (up to 3 attempts)
    sql = ""
    retry_reason = ""
    for attempt in range(3):
        if attempt > 0:
            print(f"  [Retry {attempt}: {retry_reason}]")
        sql = generate_sql(q, retry_reason)
        if sql and sql.strip().upper().startswith("SELECT"):
            break
        retry_reason = (
            "You returned JSON, explanations, or multiple SQL statements. "
            "Output ONLY a single raw SELECT statement starting with SELECT."
        )

    if not sql:
        return "Could not generate a valid SQL query. Please rephrase your question."

    rows, err = run_query(sql)
    print(f"  [Question]: {q}")
    print(f"  [SQL executed]: {sql}")
    print(f"  [Rows returned]: {len(rows)}")
    print(f"  [First row]: {rows[0] if rows else 'EMPTY'}")

    if err:
        return f"Query error: {err}\n\nSQL tried:\n{sql}"

    # --- 0 rows: dual-table retry ---
    if not rows:
        name = extract_entity_name(q)
        if name and len(name) > 2:
            print(f"  [0 rows → dual-table retry for: '{name}']")
            result = dual_table_search(name)
            if "No records found" not in result:
                return result
        return "No results found. The name may not exist or there are no matching transactions."

    # --- Route 1: listing ---
    if is_listing_question(q):
        print("  [Route: listing]")
        return format_listing(rows)

    # --- Route 2: aggregate (COUNT/SUM/single row) → DIRECT, never LLM ---
    if is_aggregate_result(rows):
        print("  [Route: aggregate direct]")
        val = format_aggregate_directly(rows)
        if val is None:
            name = extract_entity_name(q)
            if name and len(name) > 2:
                print(f"  [NULL aggregate → dual-table retry for: '{name}']")
                result = dual_table_search(name)
                if "No records found" not in result:
                    return result
            return "No matching records found. The name may not exist or there are no transactions for this entity."
        return val

    # --- Route 3: everything else → plain formatter, NO LLM ---
    # LLM summarizer removed entirely — it was hallucinating dates,
    # adding "as of June 5 2025", and returning sorry messages for
    # real data. DB results are returned directly, always accurate.
    print("  [Route: plain format]")
    return format_rows_plain(rows)


# ---------------------------------------------------------------------------
# OPENAI-COMPATIBLE ENDPOINTS (for Open WebUI)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: str
    messages: list

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    # Extract last user message
    question = next(
        (m["content"] for m in reversed(request.messages) if m["role"] == "user"), ""
    )
    try:
        answer = answer_question(question.strip())
    except Exception as e:
        answer = f"Error: {e}"

    return {
        "id": "erpnext-001",
        "object": "chat.completion",
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
    }

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": "erpnext-mcp",
            "object": "model",
            "owned_by": "local",
        }]
    }

# ---------------------------------------------------------------------------
# ORIGINAL /ask ENDPOINT (for chat.html direct access)
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str


@app.post("/ask")
async def ask(req: AskRequest):
    q = req.question.strip()
    if not q:
        return JSONResponse({"answer": "Please enter a question."})
    try:
        answer = answer_question(q)
    except Exception as e:
        answer = f"Error: {e}"
    return JSONResponse({"answer": answer})


@app.get("/health")
async def health():
    return {"status": "ok", "model": LLM_MODEL}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "chat.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>chat.html not found — place it in the same folder as web_server.py</h1>"
    )