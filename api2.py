from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel
import pymysql
import ollama
import re
import time

from config import DB_CONFIG, LLM_MODEL, OLLAMA_BASE_URL
from rag_retriever import initialize_rag, get_relevant_examples, add_example


# ---------------------------------------------------------------------------
# FASTAPI LIFESPAN — initialize RAG on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Startup] Initializing semantic RAG …")
    initialize_rag()
    print("[Startup] RAG ready.")
    yield

app = FastAPI(lifespan=lifespan)
client = ollama.Client(host=OLLAMA_BASE_URL)


class ChatRequest(BaseModel):
    model: str
    messages: list


# ---------------------------------------------------------------------------
# DB CONNECTION
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
            print(f"  [Connection attempt {attempt+1} failed: {e}]")
            time.sleep(2)
    raise Exception("Could not connect to MariaDB.")


# ---------------------------------------------------------------------------
# ALLOWED TABLES
# ---------------------------------------------------------------------------

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
# DYNAMIC SCHEMA FETCHER (cached, refreshes every 5 min)
# ---------------------------------------------------------------------------

_schema_cache: dict = {}
_schema_fetched_at: float = 0
SCHEMA_CACHE_TTL = 300


def get_schema(tables: list) -> str:
    global _schema_cache, _schema_fetched_at

    now = time.time()
    if _schema_cache and (now - _schema_fetched_at) < SCHEMA_CACHE_TTL:
        print("  [Schema: using cache]")
        return "\n".join(_schema_cache.get(t, "") for t in tables if t in _schema_cache)

    print("  [Schema: fetching from DB]")
    conn = get_connection()
    schema_lines = []
    try:
        cursor = conn.cursor()
        for table in tables:
            try:
                cursor.execute(f"SHOW COLUMNS FROM `{table}`")
                cols = cursor.fetchall()
                col_details = ", ".join(
                    f"{row['Field']} ({row['Type']})" for row in cols
                )
                line = f"Table `{table}` columns: {col_details}"
                _schema_cache[table] = line
                schema_lines.append(line)
            except Exception as e:
                print(f"  [Schema fetch failed for {table}: {e}]")
        _schema_fetched_at = now
        return "\n".join(schema_lines)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TABLE NAME FIXER
# ---------------------------------------------------------------------------

def fix_table_names(sql: str) -> str:
    replacements = {
        r'\btabSalesInvoice\b':                  '`tabSales Invoice`',
        r'\btabPurchaseInvoice\b':               '`tabPurchase Invoice`',
        r'\btabPurchaseInvoiceItem\b':           '`tabPurchase Invoice Item`',
        r'\btabSalesInvoiceItem\b':              '`tabSales Invoice Item`',
        r'\btabPaymentEntry\b':                  '`tabPayment Entry`',
        r'\btabJournalEntry\b':                  '`tabJournal Entry`',
        r'\btabStockEntry\b':                    '`tabStock Entry`',
        r'\btabStockLedgerEntry\b':              '`tabStock Ledger Entry`',
        r'(?<!`)tabSales Invoice Item(?!`)':     '`tabSales Invoice Item`',
        r'(?<!`)tabPurchase Invoice Item(?!`)':  '`tabPurchase Invoice Item`',
        r'(?<!`)tabSales Invoice(?!`)':          '`tabSales Invoice`',
        r'(?<!`)tabPurchase Invoice(?!`)':       '`tabPurchase Invoice`',
        r'(?<!`)tabPayment Entry(?!`)':          '`tabPayment Entry`',
        r'(?<!`)tabJournal Entry(?!`)':          '`tabJournal Entry`',
        r'(?<!`)tabStock Entry(?!`)':            '`tabStock Entry`',
        r'(?<!`)tabStock Ledger Entry(?!`)':     '`tabStock Ledger Entry`',
        r'(?<!`)tabSupplier(?!`)':               '`tabSupplier`',
        r'(?<!`)tabCustomer(?!`)':               '`tabCustomer`',
        r'(?<!`)tabItem(?!`)':                   '`tabItem`',
        r'(?<!`)tabEmployee(?!`)':               '`tabEmployee`',
        r'(?<!`)tabBin(?!`)':                    '`tabBin`',
    }
    for pattern, replacement in replacements.items():
        sql = re.sub(pattern, replacement, sql)
    return sql


# ---------------------------------------------------------------------------
# SQL EXTRACTOR
# ---------------------------------------------------------------------------

def extract_sql(raw: str) -> str:
    print(f"  [Raw LLM output]:\n{raw}\n")

    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

    stripped = raw.strip()
    if stripped.startswith('{') or stripped.startswith('['):
        print("  [Rejected: model returned JSON instead of SQL]")
        return ""

    fenced = re.search(r'```(?:sql)?\s*(SELECT\b.+?)```', raw, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip().rstrip(';').strip()

    select_to_blank = re.search(r'(SELECT\b.+?)(?=\n\s*\n)', raw, re.IGNORECASE | re.DOTALL)
    if select_to_blank:
        return select_to_blank.group(1).strip().rstrip(';').strip()

    fallback = re.search(r'(SELECT\b.+)', raw, re.IGNORECASE | re.DOTALL)
    if fallback:
        return fallback.group(1).strip().rstrip(';').strip()

    print("  [Could not extract SQL]")
    return ""


# ---------------------------------------------------------------------------
# INTENT DETECTION
# ---------------------------------------------------------------------------

SUPERLATIVE_KEYWORDS = ["most", "highest", "largest", "maximum", "max",
                         "least", "lowest", "smallest", "minimum", "min",
                         "best", "worst", "top", "bottom"]
LISTING_KEYWORDS     = ["all", "list", "show", "give", "every", "details", "detail"]
LIMITING_KEYWORDS    = ["oldest", "newest", "latest", "first", "last", "recent"]


def is_superlative_question(q): return any(k in q.lower() for k in SUPERLATIVE_KEYWORDS)
def is_listing_question(q):     return any(k in q.lower() for k in LISTING_KEYWORDS)
def is_limiting_question(q):    return any(k in q.lower() for k in LIMITING_KEYWORDS)


def is_ambiguous_entity_question(question: str) -> bool:
    """
    Returns True if the question references an entity name but has no clear
    indicator of whether it is a customer (sales) or a supplier (purchase).
    In that case a cross-table retry is warranted when the first attempt is empty.
    """
    lower = question.lower()
    has_context_hint = any(
        k in lower for k in ["by", "from", "for", "of", "paid", "received", "invoice"]
    )
    has_sales_hint = any(
        k in lower for k in ["customer", "sales invoice", "sold to", "receivable"]
    )
    has_purchase_hint = any(
        k in lower for k in ["supplier", "purchase invoice", "bought from", "payable"]
    )
    return has_context_hint and not has_sales_hint and not has_purchase_hint


# ---------------------------------------------------------------------------
# INTENT VALIDATOR
# ---------------------------------------------------------------------------

def validate_sql_intent(question: str, sql: str) -> tuple[bool, str]:
    sql_upper = sql.upper()
    if is_superlative_question(question) and not is_listing_question(question):
        if "LIMIT" not in sql_upper:
            return False, "Question asks for a single best/worst result but SQL is missing LIMIT 1."
        if "ORDER BY" not in sql_upper:
            return False, "Question asks for most/least but SQL is missing ORDER BY."
    return True, ""


# ---------------------------------------------------------------------------
# SQL GENERATOR — dynamic schema + semantic RAG examples
# ---------------------------------------------------------------------------

def generate_sql(question: str, retry_reason: str = "") -> str:
    schema    = get_schema(ALLOWED_TABLES)
    examples  = get_relevant_examples(question)          # ← semantic RAG
    table_list = "\n".join(f"  - `{t}`" for t in ALLOWED_TABLES)

    retry_block = ""
    if retry_reason:
        retry_block = (
            f"\nPREVIOUS ATTEMPT FAILED — reason: {retry_reason}\n"
            f"Fix the issue and regenerate. Output ONLY the corrected SQL.\n"
        )

    system_prompt = """You are a MariaDB SQL query generator for ERPNext.

YOUR ONLY JOB: Output a single raw SQL SELECT statement. Nothing else.

ABSOLUTE OUTPUT RULES:
- Output ONLY the SQL query. No JSON. No explanations. No markdown. No code fences. No multiple alternatives.
- Do NOT output {\"follow_ups\": ...} or any JSON object. Ever.
- Do NOT write multiple SQL options — pick the best one and output only that.
- The response must start with SELECT and end with a semicolon. Nothing before SELECT. Nothing after the semicolon.
- If you cannot generate SQL, output exactly: SELECT 1;"""

    user_prompt = f"""STRICT SQL RULES:
1. Wrap ALL table names and column names in backticks.
2. Always filter `docstatus` = 1 for transactional tables.
3. Use `supplier` column in `tabPurchase Invoice` for supplier name.
4. Use `customer` column in `tabSales Invoice` for customer name.
5. For item-level details use child tables: `tabPurchase Invoice Item` or `tabSales Invoice Item`.
6. Use `outstanding_amount` column directly — never calculate manually.
7. "most"/"highest"/"largest"/"maximum" → ORDER BY ... DESC LIMIT 1
8. "least"/"lowest"/"smallest"/"minimum" → ORDER BY ... ASC LIMIT 1
9. "top N" → ORDER BY ... DESC LIMIT N
10. For oldest: ORDER BY `posting_date` ASC LIMIT 1. For newest/latest: ORDER BY `posting_date` DESC LIMIT 1.
11. NEVER use UNION or UNION ALL.
12. When "invoices" has no purchase/sales context, default to `tabSales Invoice`.
13. For overdue: `status` = 'Overdue' AND `docstatus` = 1
14. For "unpaid": `status` IN ('Unpaid', 'Overdue') AND `docstatus` = 1
15. NEVER use `outstanding_amount` > 0 alone to filter "unpaid".
16. Supplier name filter: ALWAYS use LIKE e.g. WHERE `supplier` LIKE '%name%'
17. Customer name filter: ALWAYS use LIKE e.g. WHERE `customer` LIKE '%name%'
18. For tax amount: use `total_taxes_and_charges` column.
21. When unsure if name is customer or supplier, default to `tabSales Invoice`.
22. Output ONLY one SQL query — never write multiple alternatives or explanations.
23. For stock queries use `tabBin` for current stock levels.
{retry_block}
Available tables:
{table_list}

Live Database Schema:
{schema}

{examples}

Question: {question}

SQL:"""

    response = client.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        options={"temperature": 0},
    )

    raw = response["message"]["content"].strip()
    sql = extract_sql(raw)
    if not sql:
        return ""

    sql = fix_table_names(sql)

    if not is_superlative_question(question) and not is_limiting_question(question):
        sql = re.sub(r'\bLIMIT\s+\d+\b', '', sql, flags=re.IGNORECASE).strip()

    sql = re.sub(r'\s+', ' ', sql).strip().rstrip(';').strip()
    print(f"  [Final SQL]: {sql}")
    return sql


# ---------------------------------------------------------------------------
# SQL WHITELIST VALIDATOR
# ---------------------------------------------------------------------------

def validate_sql(sql: str) -> None:
    quoted = re.findall(r'`([^`]+)`', sql)
    used_tables = [q for q in quoted if q.lower().startswith('tab')]
    for t in used_tables:
        if t not in ALLOWED_TABLES:
            raise Exception(f"Table `{t}` not in allowed list.\nSQL: {sql}")


# ---------------------------------------------------------------------------
# SQL RUNNER
# ---------------------------------------------------------------------------

def run_sql(sql: str) -> list:
    if not sql.strip().upper().startswith("SELECT"):
        raise Exception("Only SELECT queries are allowed.")
    validate_sql(sql)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        print(f"  [Row count]: {len(rows)}")
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RESULT FORMATTERS
# ---------------------------------------------------------------------------

SKIP_FIELDS = {
    "creation", "modified", "modified_by", "owner", "docstatus", "naming_series",
    "idx", "posting_time", "set_posting_time", "conversion_rate", "buying_price_list",
    "price_list_currency", "plc_conversion_rate", "base_total", "base_net_total",
    "base_taxes_and_charges_added", "base_total_taxes_and_charges", "base_grand_total",
    "base_rounding_adjustment", "base_rounded_total", "base_in_words",
    "tax_withholding_net_total", "base_tax_withholding_net_total",
    "taxes_and_charges_added", "apply_discount_on", "other_charges_calculation",
    "party_account_currency", "is_opening", "language", "supplier_group",
    "taxes_and_charges", "tally_guid", "tally_voucher_type", "tally_voucher_number",
    "gst_vehicle_type", "itc_classification", "gst_category", "mode_of_transport",
    "lr_date", "against_expense_account", "title", "payment_terms_template",
}


def format_results(rows: list) -> str:
    if not rows:
        return "No results found."
    return "\n".join(
        ", ".join(f"{k}: {v}" for k, v in row.items() if v is not None and v != "")
        for row in rows
    )


def format_results_as_answer(rows: list) -> str:
    if not rows:
        return "No results found."
    lines = [f"Found {len(rows)} result(s):\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"--- {i} ---")
        for k, v in row.items():
            if k in SKIP_FIELDS or v is None or v == "" or v == 0:
                continue
            if isinstance(v, float):
                v = f"₹{v:,.2f}"
            lines.append(f"  {k}: {v}")
        lines.append("")
    return "\n".join(lines)


AGGREGATE_KEYS = {
    "count", "total", "sum", "avg", "average", "max", "min", "amount", "balance",
    "tax", "due", "payable", "receivable", "purchases", "sales", "payments",
    "received", "paid", "qty", "employees", "customers", "suppliers",
    "invoices", "stock",
}


def is_aggregate_result(rows: list) -> bool:
    if len(rows) != 1:
        return False
    keys = [k.lower() for k in rows[0].keys()]
    return len(keys) <= 2 and any(agg in k for k in keys for agg in AGGREGATE_KEYS)


def format_aggregate_directly(rows: list) -> str:
    row = rows[0]
    parts = []
    all_null = True
    for k, v in row.items():
        if v is None:
            continue
        all_null = False
        parts.append(f"₹{v:,.2f}" if isinstance(v, float) else str(v))

    if all_null or not parts:
        return (
            "No matching records found. "
            "The name may not exist in the database, or there are no transactions for this entity."
        )
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# LLM SUMMARIZER (Pass 2 — non-aggregate only)
# ---------------------------------------------------------------------------

def summarize_with_llm(question: str, rows: list) -> str:
    db_result = format_results(rows)

    summary_prompt = f"""You are a helpful ERP assistant. Answer using ONLY the data below.

User question: {question}

Database result:
{db_result}

RULES:
- Use ONLY numbers and names from the database result. Never invent values.
- Show monetary amounts as ₹ with comma formatting.
- Show dates in readable format (e.g. "June 5, 2025").
- Be concise — one or two sentences.
- No markdown, no bullet points.

Answer:"""

    response = client.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "You are a factual ERP assistant. Use ONLY data from the database result. Never invent numbers."},
            {"role": "user",   "content": summary_prompt},
        ],
        options={"temperature": 0},
    )
    answer = response["message"]["content"]
    return re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# CROSS-TABLE RETRY HELPER
# ---------------------------------------------------------------------------

def attempt_cross_table_retry(question: str, original_sql: str) -> tuple[list, str]:
    """
    If the original query returned no rows and the question is ambiguous
    (no clear customer/supplier signal), flip between tabSales Invoice and
    tabPurchase Invoice and try again.

    Returns (rows, sql) — both empty/unchanged if retry not applicable or also empty.
    """
    sql_upper = original_sql.upper()

    uses_sales    = "`TABSALES INVOICE`"    in sql_upper
    uses_purchase = "`TABPURCHASE INVOICE`" in sql_upper

    # Only retry when exactly one table is in play
    if uses_sales and not uses_purchase:
        flipped_table   = "tabPurchase Invoice"
        flipped_entity  = "supplier"
        original_table  = "tabSales Invoice"
    elif uses_purchase and not uses_sales:
        flipped_table   = "tabSales Invoice"
        flipped_entity  = "customer"
        original_table  = "tabPurchase Invoice"
    else:
        return [], original_sql   # both or neither — don't retry

    print(f"  [Cross-table retry: {original_table} was empty → trying {flipped_table}]")

    cross_retry_reason = (
        f"The previous query on `{original_table}` returned NO results. "
        f"The entity name might be a {flipped_entity} in `{flipped_table}` instead. "
        f"Rewrite the query using `{flipped_table}` with the `{flipped_entity}` column. "
        f"Keep all other logic (aggregation, filters, date ranges) identical."
    )

    fallback_sql = generate_sql(question, retry_reason=cross_retry_reason)

    if not fallback_sql or not fallback_sql.strip().upper().startswith("SELECT"):
        print("  [Cross-table retry: LLM did not produce valid SQL]")
        return [], original_sql

    try:
        fallback_rows = run_sql(fallback_sql)
        if fallback_rows:
            print(f"  [Cross-table retry succeeded with {flipped_table}: {len(fallback_rows)} row(s)]")
            return fallback_rows, fallback_sql
        else:
            print("  [Cross-table retry also returned no results]")
            return [], original_sql
    except Exception as e:
        print(f"  [Cross-table retry SQL error: {e}]")
        return [], original_sql


# ---------------------------------------------------------------------------
# MAIN ORCHESTRATOR
# ---------------------------------------------------------------------------

MAX_RETRIES = 2


def answer_question(question: str) -> str:
    sql = ""
    retry_reason = ""

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            print(f"  [Retry {attempt} — reason: {retry_reason}]")

        sql = generate_sql(question, retry_reason=retry_reason)

        if not sql or not sql.strip().upper().startswith("SELECT"):
            retry_reason = (
                "You returned JSON, explanations, or multiple alternatives. "
                "Output ONLY a single raw SQL SELECT. Start with SELECT, end with semicolon."
            )
            continue

        is_valid, reason = validate_sql_intent(question, sql)
        if not is_valid:
            retry_reason = reason
            continue

        retry_reason = ""
        break

    if not sql or not sql.strip().upper().startswith("SELECT"):
        return "Could not generate a valid SQL query. Please rephrase your question."

    try:
        rows = run_sql(sql)
    except Exception as e:
        return f"SQL error: {e}\nSQL was: {sql}"

    # -----------------------------------------------------------------------
    # CROSS-TABLE RETRY: no rows + ambiguous entity → try the other table
    # -----------------------------------------------------------------------
    if not rows and is_ambiguous_entity_question(question):
        rows, sql = attempt_cross_table_retry(question, sql)
    # -----------------------------------------------------------------------

    if not rows:
        return "No results found for your query."

    if is_listing_question(question):
        return format_results_as_answer(rows)

    if is_aggregate_result(rows):
        print("  [Route: aggregate direct]")
        return format_aggregate_directly(rows)

    print("  [Route: LLM summarizer]")
    return summarize_with_llm(question, rows)


# ---------------------------------------------------------------------------
# FASTAPI ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ERPNext Text-to-SQL API (Semantic RAG) is running"}


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest):
    question = next(
        (m["content"] for m in reversed(request.messages) if m["role"] == "user"), ""
    )
    try:
        answer = answer_question(question)
    except Exception as e:
        answer = f"Error: {e}"

    return {
        "id": "erpnext-rag",
        "object": "chat.completion",
        "model": request.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
    }


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "erpnext-invoices", "object": "model", "owned_by": "local"}]}