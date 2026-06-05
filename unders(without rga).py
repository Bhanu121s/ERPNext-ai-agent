from fastapi import FastAPI
from pydantic import BaseModel
import pymysql
import ollama
import re
import time
import json
import os
from config import DB_CONFIG, LLM_MODEL, OLLAMA_BASE_URL

app = FastAPI()
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
# ALLOWED TABLES — expanded with child tables
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
# DYNAMIC SCHEMA FETCHER
# Fetches live column names + types from MariaDB at runtime.
# This means the prompt always reflects the real DB — no hardcoding.
# ---------------------------------------------------------------------------

_schema_cache: dict = {}   # cache so we don't hit DB on every request
_schema_fetched_at: float = 0
SCHEMA_CACHE_TTL = 300     # refresh schema every 5 minutes


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
# DYNAMIC EXAMPLE RETRIEVAL
# Loads examples from examples.json and picks the top-K most relevant
# ones for the current question using simple word-overlap similarity.
# To add new examples: just edit examples.json — no code changes needed.
# ---------------------------------------------------------------------------

EXAMPLES_FILE = os.path.join(os.path.dirname(__file__), "examples.json")
_examples_cache: list = []


def load_examples() -> list:
    global _examples_cache
    if _examples_cache:
        return _examples_cache
    try:
        with open(EXAMPLES_FILE, "r", encoding="utf-8") as f:
            _examples_cache = json.load(f)
        print(f"  [Examples: loaded {len(_examples_cache)} from {EXAMPLES_FILE}]")
    except Exception as e:
        print(f"  [Examples: failed to load — {e}]")
        _examples_cache = []
    return _examples_cache


def word_overlap_score(q1: str, q2: str) -> float:
    """Simple word overlap similarity — no external libraries needed."""
    stop_words = {"i", "me", "my", "the", "a", "an", "do", "did", "does",
                  "have", "has", "is", "are", "was", "were", "to", "for",
                  "of", "in", "on", "at", "how", "what", "which", "who",
                  "much", "many", "all", "any", "some", "this", "that"}
    w1 = set(re.findall(r'\w+', q1.lower())) - stop_words
    w2 = set(re.findall(r'\w+', q2.lower())) - stop_words
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


def get_relevant_examples(question: str, top_k: int = 4) -> str:
    examples = load_examples()
    if not examples:
        return ""

    scored = [
        (word_overlap_score(question, ex["question"]), ex)
        for ex in examples
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [ex for score, ex in scored[:top_k] if score > 0]

    if not top:
        # fallback: return first 3 examples if no overlap found
        top = examples[:3]

    lines = ["--- RELEVANT EXAMPLES (learn the pattern) ---"]
    for ex in top:
        lines.append(f"Q: {ex['question']}")
        lines.append(f"A: {ex['sql']}\n")
    lines.append("--- END OF EXAMPLES ---")
    return "\n".join(lines)


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

    # Strip <think> blocks (Qwen3 thinking mode)
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

    # Reject JSON output immediately
    stripped = raw.strip()
    if stripped.startswith('{') or stripped.startswith('['):
        print("  [Rejected: model returned JSON instead of SQL]")
        return ""

    # Extract first SQL block from markdown fence
    fenced = re.search(r'```(?:sql)?\s*(SELECT\b.+?)```', raw, re.IGNORECASE | re.DOTALL)
    if fenced:
        sql = fenced.group(1).strip().rstrip(';').strip()
        print(f"  [Extracted from fence]: {sql}")
        return sql

    # SELECT up to first blank line
    select_to_blank = re.search(r'(SELECT\b.+?)(?=\n\s*\n)', raw, re.IGNORECASE | re.DOTALL)
    if select_to_blank:
        sql = select_to_blank.group(1).strip().rstrip(';').strip()
        print(f"  [Extracted via blank-line boundary]: {sql}")
        return sql

    # Fallback: SELECT to end of string
    fallback = re.search(r'(SELECT\b.+)', raw, re.IGNORECASE | re.DOTALL)
    if fallback:
        sql = fallback.group(1).strip().rstrip(';').strip()
        print(f"  [Extracted via fallback]: {sql}")
        return sql

    print("  [Could not extract SQL]")
    return ""


# ---------------------------------------------------------------------------
# INTENT DETECTOR
# ---------------------------------------------------------------------------

SUPERLATIVE_KEYWORDS = [
    "most", "highest", "largest", "maximum", "max",
    "least", "lowest", "smallest", "minimum", "min",
    "best", "worst", "top", "bottom",
]
LISTING_KEYWORDS = ["all", "list", "show", "give", "every", "details", "detail"]
LIMITING_KEYWORDS = ["oldest", "newest", "latest", "first", "last", "recent"]


def is_superlative_question(question: str) -> bool:
    return any(k in question.lower() for k in SUPERLATIVE_KEYWORDS)

def is_listing_question(question: str) -> bool:
    return any(k in question.lower() for k in LISTING_KEYWORDS)

def is_limiting_question(question: str) -> bool:
    return any(k in question.lower() for k in LIMITING_KEYWORDS)


# ---------------------------------------------------------------------------
# SQL INTENT VALIDATOR
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
# SQL GENERATOR — dynamic schema + dynamic examples
# ---------------------------------------------------------------------------

def generate_sql(question: str, retry_reason: str = "") -> str:
    # Fetch live schema from DB
    schema = get_schema(ALLOWED_TABLES)

    # Retrieve relevant examples dynamically
    examples = get_relevant_examples(question)

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
- Do NOT output {"follow_ups": ...} or any JSON object. Ever.
- Do NOT write multiple SQL options — pick the best one and output only that.
- The response must start with SELECT and end with a semicolon. Nothing before SELECT. Nothing after the semicolon.
- If you cannot generate SQL, output exactly: SELECT 1;"""

    user_prompt = f"""STRICT SQL RULES:
1. Wrap ALL table names and column names in backticks.
2. Always filter `docstatus` = 1 for transactional tables (tabPurchase Invoice, tabSales Invoice, tabPayment Entry, tabStock Entry, tabJournal Entry).
3. Use `supplier` column in `tabPurchase Invoice` for supplier name.
4. Use `customer` column in `tabSales Invoice` for customer name.
5. For item-level details use child tables: `tabPurchase Invoice Item` (parent = invoice name) or `tabSales Invoice Item`.
6. For outstanding amount use `outstanding_amount` column directly. Never calculate manually.
7. "most"/"highest"/"largest"/"maximum" → ORDER BY ... DESC LIMIT 1
8. "least"/"lowest"/"smallest"/"minimum" → ORDER BY ... ASC LIMIT 1
9. "top N" → ORDER BY ... DESC LIMIT N
10. For oldest: ORDER BY `posting_date` ASC LIMIT 1. For newest/latest: ORDER BY `posting_date` DESC LIMIT 1.
11. NEVER use UNION or UNION ALL.
12. When "invoices" has no purchase/sales context, default to `tabSales Invoice`.
13. For overdue: `status` = 'Overdue' AND `docstatus` = 1
14. For "unpaid": `status` IN ('Unpaid', 'Overdue') AND `docstatus` = 1
15. NEVER use `outstanding_amount` > 0 alone to filter "unpaid" — always use `status` IN ('Unpaid', 'Overdue').
16. Supplier name filter: ALWAYS use LIKE e.g. WHERE `supplier` LIKE '%name%'
17. Customer name filter: ALWAYS use LIKE e.g. WHERE `customer` LIKE '%name%'
18. For tax amount: use `total_taxes_and_charges` column.
19. "how much tax did X pay" where X is customer → `tabSales Invoice` WHERE `customer` LIKE '%X%'.
20. "how much tax did X pay" where X is supplier → `tabPurchase Invoice` WHERE `supplier` LIKE '%X%'.
21. When unsure if name is customer or supplier, default to `tabSales Invoice` and `customer` LIKE.
22. Output ONLY one SQL query — never write multiple alternatives or explanations.
23. For stock queries use `tabBin` for current stock levels.
24. `tabPurchase Invoice Item` and `tabSales Invoice Item` also have `docstatus` inherited from parent — filter via parent join or use the child table's own docstatus if available.
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

    # Strip LIMIT only for pure listing questions (not superlative, not time-limiting)
    if not is_superlative_question(question) and not is_limiting_question(question):
        sql = re.sub(r'\bLIMIT\s+\d+\b', '', sql, flags=re.IGNORECASE).strip()

    sql = re.sub(r'\s+', ' ', sql).strip()
    sql = sql.rstrip(';').strip()

    print(f"  [Final SQL]: {sql}")
    return sql


# ---------------------------------------------------------------------------
# SQL WHITELIST VALIDATOR
# ---------------------------------------------------------------------------

def validate_sql(sql: str) -> None:
    quoted = re.findall(r'`([^`]+)`', sql)
    used_tables = [q for q in quoted if q.lower().startswith('tab') or q.lower() == 'tabbin']
    for t in used_tables:
        if t not in ALLOWED_TABLES:
            raise Exception(
                f"Table `{t}` is not in the allowed list.\n"
                f"Allowed: {ALLOWED_TABLES}\nSQL: {sql}"
            )


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
    "creation", "modified", "modified_by", "owner", "docstatus",
    "naming_series", "idx", "posting_time", "set_posting_time",
    "update_outstanding_for_self", "conversion_rate", "buying_price_list",
    "price_list_currency", "plc_conversion_rate", "base_total", "base_net_total",
    "base_taxes_and_charges_added", "base_total_taxes_and_charges",
    "base_grand_total", "base_rounding_adjustment", "base_rounded_total",
    "base_in_words", "tax_withholding_net_total", "base_tax_withholding_net_total",
    "taxes_and_charges_added", "apply_discount_on", "other_charges_calculation",
    "party_account_currency", "is_opening", "language", "supplier_group",
    "taxes_and_charges", "tally_guid", "tally_voucher_type", "tally_voucher_number",
    "gst_vehicle_type", "itc_classification", "gst_category",
    "mode_of_transport", "lr_date", "against_expense_account",
    "title", "payment_terms_template",
}


def format_results(rows: list) -> str:
    if not rows:
        return "No results found."
    result = []
    for row in rows:
        fields = ", ".join(
            f"{k}: {v}" for k, v in row.items() if v is not None and v != ""
        )
        result.append(fields)
    return "\n".join(result)


def format_results_as_answer(rows: list) -> str:
    if not rows:
        return "No results found."
    total = len(rows)
    lines = [f"Found {total} result(s):\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"--- {i} ---")
        for k, v in row.items():
            if k in SKIP_FIELDS:
                continue
            if v is None or v == "" or v == 0:
                continue
            if isinstance(v, float):
                v = f"₹{v:,.2f}"
            lines.append(f"  {k}: {v}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AGGREGATE RESULT DETECTOR
# ---------------------------------------------------------------------------

AGGREGATE_KEYS = {
    "count", "total", "sum", "avg", "average", "max", "min",
    "amount", "balance", "tax", "due", "payable", "receivable",
    "purchases", "sales", "payments", "received", "paid", "qty",
    "employees", "customers", "suppliers", "invoices", "stock",
}


def is_aggregate_result(rows: list) -> bool:
    if len(rows) != 1:
        return False
    keys = [k.lower() for k in rows[0].keys()]
    if len(keys) <= 2 and any(agg in k for k in keys for agg in AGGREGATE_KEYS):
        return True
    return False


def format_aggregate_directly(rows: list) -> str:
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
        print("  [Aggregate result was NULL]")
        return (
            "No matching records found. "
            "The name may not exist in the database, or there are no transactions for this entity."
        )

    value = ", ".join(parts)
    print(f"  [Aggregate direct format]: {value}")
    return value


# ---------------------------------------------------------------------------
# LLM SUMMARIZER — Pass 2 (only for non-aggregate results)
# ---------------------------------------------------------------------------

def summarize_with_llm(question: str, rows: list) -> str:
    db_result = format_results(rows)

    summary_prompt = f"""You are a helpful ERP assistant. Answer the user's question using ONLY the exact data below.

User question: {question}

Database result:
{db_result}

CRITICAL RULES:
- Use ONLY the numbers and names from the database result above. No exceptions.
- Do NOT use any number from your memory or training data.
- Show monetary amounts in Indian Rupees (₹) with comma formatting.
- Show dates in a readable format like "June 5, 2025".
- Be concise — one or two sentences maximum.
- No markdown, no bullet points, no headers.

Answer:"""

    response = client.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "You are a factual ERP assistant. You ONLY use data from the database result provided. You NEVER invent numbers from memory."},
            {"role": "user",   "content": summary_prompt},
        ],
        options={"temperature": 0},
    )
    answer = response["message"]["content"]
    answer = re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()
    return answer


# ---------------------------------------------------------------------------
# MAIN ORCHESTRATOR
# ---------------------------------------------------------------------------

MAX_RETRIES = 2


def answer_question(question: str) -> str:
    sql = ""
    retry_reason = ""

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            print(f"  [Retry attempt {attempt} — reason: {retry_reason}]")

        sql = generate_sql(question, retry_reason=retry_reason)

        if not sql or not sql.strip().upper().startswith("SELECT"):
            retry_reason = (
                "You returned JSON, explanations, or multiple SQL alternatives. "
                "Output ONLY a single raw SQL SELECT statement. "
                "Start with SELECT, end with semicolon. Nothing else."
            )
            continue

        is_valid, reason = validate_sql_intent(question, sql)
        if not is_valid:
            retry_reason = reason
            print(f"  [Intent validation failed]: {reason}")
            continue

        retry_reason = ""
        break

    if not sql or not sql.strip().upper().startswith("SELECT"):
        return (
            "Could not generate a valid SQL query after multiple attempts. "
            "Please try rephrasing your question."
        )

    if retry_reason:
        print(f"  [Warning: proceeding despite issue: {retry_reason}]")

    try:
        rows = run_sql(sql)
    except Exception as e:
        return f"SQL error: {e}\nGenerated SQL was: {sql}"

    if not rows:
        return "No results found for your query."

    # Route 1: listing → detailed formatter
    if is_listing_question(question):
        return format_results_as_answer(rows)

    # Route 2: aggregate (COUNT, SUM, etc.) → return directly, never through LLM
    if is_aggregate_result(rows):
        print("  [Route: aggregate direct]")
        return format_aggregate_directly(rows)

    # Route 3: everything else → LLM summarizer
    print("  [Route: LLM summarizer]")
    return summarize_with_llm(question, rows)


# ---------------------------------------------------------------------------
# FASTAPI ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ERPNext Text-to-SQL API is running"}


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest):
    question = next(
        (m["content"] for m in reversed(request.messages) if m["role"] == "user"),
        ""
    )
    try:
        answer = answer_question(question)
    except Exception as e:
        answer = f"Error: {e}"

    return {
        "id": "erpnext-rag",
        "object": "chat.completion",
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": answer,
            },
            "finish_reason": "stop",
        }],
    }


@app.get("/v1/models")
async def models():
    return {
        "object": "list",
        "data": [{
            "id": "erpnext-invoices",
            "object": "model",
            "owned_by": "local",
        }],
    }