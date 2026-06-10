"""
mcp_server.py 
Tools:
  - get_schema      : fetch live column names for any table
  - run_sql         : execute a SELECT query
  - search_entity   : dual-table search (customer + supplier simultaneously)
  - get_summary     : pre-built business dashboard
  - get_top         : pre-built top customers/suppliers ranking
  - get_trend       : pre-built monthly trend
"""

from fastmcp import FastMCP
import pymysql
import re
import time

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host":     "172.28.157.72",
    "port":     3306,
    "user":     "root",
    "password": "root@123",
    "database": "_b9e0741af4689f11",
    "charset":  "utf8mb4",
}

ALLOWED_TABLES = [
    "tabPurchase Invoice",
    "tabPurchase Invoice Item",
    "tabSales Invoice",
    "tabSales Invoice Item",
    "tabSupplier",
    "tabCustomer",
    "tabItem List",
    "tabItem",
    "tabEmployee",
    "tabPayment Entry",
    "tabJournal Entry",
    "tabStock Entry",
    "tabStock Ledger Entry",
    "tabBin",
]

mcp = FastMCP("ERPNext")

# ---------------------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------------------

def get_connection():
    for attempt in range(3):
        try:
            return pymysql.connect(
                **DB_CONFIG,
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10,
                read_timeout=30,
            )
        except Exception as e:
            print(f"  [DB attempt {attempt+1} failed: {e}]")
            time.sleep(1)
    raise Exception("Cannot connect to MariaDB.")


def _execute(query: str) -> tuple:
    """Returns (rows, error). Safe â€” SELECT only, whitelist enforced."""
    q = query.strip()
    if not q.upper().startswith("SELECT"):
        return [], "Only SELECT queries allowed."

    # Whitelist check
    quoted = re.findall(r'`([^`]+)`', q)
    for t in [x for x in quoted if x.lower().startswith("tab")]:
        if t not in ALLOWED_TABLES:
            return [], f"Table `{t}` not in allowed list."

    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(q)
        rows = cur.fetchall()
        conn.close()
        return rows, ""
    except Exception as e:
        return [], f"SQL error: {e}"


def _fmt(rows: list) -> str:
    if not rows:
        return "NO_RESULTS"
    lines = []
    for i, row in enumerate(rows, 1):
        parts = []
        for k, v in row.items():
            if v is None or v == "":
                continue
            parts.append(f"{k}: â‚¹{v:,.2f}" if isinstance(v, float) else f"{k}: {v}")
        lines.append(f"{i}. " + " | ".join(parts))
    return f"{len(rows)} row(s):\n" + "\n".join(lines)

# ---------------------------------------------------------------------------
# SCHEMA CACHE (refreshes every 5 min)
# ---------------------------------------------------------------------------

_schema_cache: dict = {}
_schema_at: float   = 0
SCHEMA_TTL          = 300


def _get_full_schema() -> str:
    global _schema_cache, _schema_at
    now = time.time()
    if _schema_cache and (now - _schema_at) < SCHEMA_TTL:
        return "\n".join(_schema_cache.values())
    try:
        conn = get_connection()
        cur  = conn.cursor()
        out  = {}
        for table in ALLOWED_TABLES:
            try:
                cur.execute(f"SHOW COLUMNS FROM `{table}`")
                cols = cur.fetchall()
                out[table] = f"`{table}`: " + ", ".join(
                    f"{r['Field']}({r['Type']})" for r in cols
                )
            except:
                pass
        conn.close()
        _schema_cache = out
        _schema_at    = now
        return "\n".join(out.values())
    except Exception as e:
        return f"Schema error: {e}"


# ---------------------------------------------------------------------------
# TOOL 1 â€” get_schema
# ---------------------------------------------------------------------------

@mcp.tool()
def get_schema(table_name: str) -> str:
    """
    Get live column names and types for an ERPNext table from MariaDB.
    Always call this before writing SQL to confirm exact column names.

    Pass one of these table names:
      tabPurchase Invoice, tabPurchase Invoice Item,
      tabSales Invoice, tabSales Invoice Item,
      tabSupplier, tabCustomer, tabItem, tabEmployee,
      tabPayment Entry, tabJournal Entry,
      tabStock Entry, tabStock Ledger Entry, tabBin

    CRITICAL COLUMN REFERENCE (memorize â€” avoids wrong column names):

    tabPurchase Invoice:
      name, supplier, grand_total, outstanding_amount,
      total_taxes_and_charges, status, docstatus,
      posting_date, due_date, bill_no, bill_date

    tabSales Invoice:
      name, customer, grand_total, outstanding_amount,
      total_taxes_and_charges, status, docstatus,
      posting_date, due_date

    tabPurchase Invoice Item / tabSales Invoice Item:
      name, parent, item_code, item_name, qty, rate, amount, docstatus

    tabBin:
      item_code, warehouse, actual_qty, reserved_qty, ordered_qty

    tabPayment Entry:
      name, payment_type, paid_amount, party_type, party,
      posting_date, docstatus

    tabEmployee:
      name, employee_name, status, designation, department,
      date_of_joining, date_of_birth
    """
    if table_name not in ALLOWED_TABLES:
        return f"Error: '{table_name}' not allowed. Choose from:\n" + "\n".join(ALLOWED_TABLES)
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(f"SHOW COLUMNS FROM `{table_name}`")
        cols = cur.fetchall()
        conn.close()
        return f"`{table_name}` columns:\n" + "\n".join(
            f"  {r['Field']} ({r['Type']})" for r in cols
        )
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# TOOL 2 â€” run_sql
# ---------------------------------------------------------------------------

@mcp.tool()
def run_sql(query: str) -> str:
    """
    Execute a SELECT query on ERPNext MariaDB. Returns real data.
    Only SELECT is allowed. INSERT/UPDATE/DELETE/DROP are blocked.

    ================================================================
    NON-NEGOTIABLE SQL RULES â€” violating these causes wrong answers:
    ================================================================

    RULE 1 â€” BACKTICKS ON EVERYTHING:
      Every table name and column name must be wrapped in backticks.
      WRONG : SELECT grand_total FROM tabSales Invoice WHERE docstatus=1
      CORRECT: SELECT `grand_total` FROM `tabSales Invoice` WHERE `docstatus`=1

    RULE 2 â€” DOCSTATUS FILTER:
      All transactional tables have draft/cancelled records (docstatus 0 and 2).
      By default add: WHERE `docstatus` = 1
      This excludes drafts and cancelled invoices for normal business totals.
      If the user asks for cancelled invoices, all invoices, or a status
      breakdown including cancelled, use `docstatus` IN (1, 2) and include
      cancelled counts with `docstatus` = 2 OR `status` = 'Cancelled'.
      Never include drafts (`docstatus` = 0) unless the user explicitly asks.
      Tables needing this: tabPurchase Invoice, tabSales Invoice,
      tabPayment Entry, tabStock Entry, tabJournal Entry,
      tabPurchase Invoice Item, tabSales Invoice Item

    RULE 3 â€” SUPPLIER vs CUSTOMER TABLE (MOST CRITICAL):
      SUPPLIER â†’ ALWAYS use `tabPurchase Invoice`, column `supplier`
      CUSTOMER â†’ ALWAYS use `tabSales Invoice`, column `customer`
      NEVER query tabSales Invoice for a supplier.
      NEVER query tabPurchase Invoice for a customer.
      When unsure â†’ use search_entity tool instead of guessing.
      If the user only says "invoice/invoices" and does NOT clearly say
      sales/customer/receivable or purchase/supplier/payable, treat it as
      BOTH sales and purchase invoices. Do not default to Sales Invoice.
      For all unpaid/overdue/outstanding invoice questions, use run_sql.
      If sales vs purchase is unclear, run one Sales Invoice query and one
      Purchase Invoice query separately, then compare or combine the results.
      Use GROUP BY, COUNT(*), ORDER BY, and LIMIT for ranking questions.

    RULE 4 â€” NAME FILTERS MUST USE LIKE:
      WRONG : WHERE `supplier` = 'FMS Traders'
      CORRECT: WHERE `supplier` LIKE '%FMS Traders%'
      WRONG : WHERE `customer` = 'Golden Twist'
      CORRECT: WHERE `customer` LIKE '%Golden Twist%'

    RULE 5 â€” STATUS FILTERS:
      Overdue only         : WHERE `status` = 'Overdue'
      Unpaid only          : WHERE `status` = 'Unpaid'
      Cancelled only       : WHERE (`docstatus` = 2 OR `status` = 'Cancelled')
      Both unpaid+overdue  : WHERE `status` IN ('Unpaid', 'Overdue')
      ONLY use IN ('Unpaid','Overdue') when user says "unpaid or overdue" or "outstanding".
      When user says just "unpaid" â†’ use ONLY status = 'Unpaid'
      When user says just "overdue" â†’ use ONLY status = 'Overdue'

    RULE 6 â€” CORRECT AMOUNT COLUMNS:
      Amount still owed    â†’ `outstanding_amount`  (unpaid portion)
      Total invoice value  â†’ `grand_total`          (full amount billed)
      Tax charged          â†’ `total_taxes_and_charges`
      NEVER use `grand_total` when asking about unpaid/outstanding amounts.
      NEVER use `taxes_and_charges` â€” the correct column is `total_taxes_and_charges`.

    RULE 7 â€” NO INVENTED DATE FILTERS:
      NEVER add a date/time filter unless the user explicitly mentions
      a date, month, year, or time period.
      "how many invoices does X have" = ALL records, no date limit.
      "what does X owe" = ALL outstanding, no date limit.
      NEVER use hardcoded dates like '2025-06-05'.
      ONLY use CURDATE(), MONTH(), YEAR() when user says "this month/year".

    RULE 8 â€” RANKING / SUPERLATIVES:
      highest/most/maximum â†’ ORDER BY ... DESC LIMIT 1
      lowest/least/minimum â†’ ORDER BY ... ASC LIMIT 1
      top N                â†’ ORDER BY ... DESC LIMIT N
      oldest               â†’ ORDER BY `posting_date` ASC LIMIT 1
      newest/latest        â†’ ORDER BY `posting_date` DESC LIMIT 1
      For "who has the most unpaid/overdue invoices", use run_sql.
      The user is asking for a grouped ranking, not an invoice list.
      If sales vs purchase is unclear, run one grouped Sales Invoice query and
      one grouped Purchase Invoice query separately, then compare the top rows.

    RULE 9 â€” ITEM-LEVEL DETAILS:
      Items on a specific invoice â†’ use child table + parent filter:
        tabPurchase Invoice Item WHERE `parent` = 'PINV-XXXX'
        tabSales Invoice Item   WHERE `parent` = 'INV-XXXX'

    RULE 10 â€” STOCK:
      Current stock level â†’ `tabBin`, column `actual_qty`
      Stock movements     â†’ `tabStock Ledger Entry`

    RULE 11 â€” PAYMENTS:
      Payments sent to suppliers   â†’ tabPayment Entry WHERE `payment_type` = 'Pay'
      Payments received from customers â†’ tabPayment Entry WHERE `payment_type` = 'Receive'
      Amount column â†’ `paid_amount`

    RULE 12 â€” NO UNION:
      Never use UNION or UNION ALL. Use search_entity for cross-table lookups.

    RULE 13 â€” EMPLOYEES:
      Active employees â†’ tabEmployee WHERE `status` = 'Active'

    ================================================================
    VERIFIED CORRECT EXAMPLES â€” match these patterns exactly:
    ================================================================

    Q: how many overdue purchase invoices does FMS Traders have
    SQL: SELECT COUNT(*) AS `overdue_count`
         FROM `tabPurchase Invoice`
         WHERE `docstatus` = 1
         AND `supplier` LIKE '%FMS Traders%'
         AND `status` = 'Overdue'
    NOTE: status = 'Overdue' not IN ('Unpaid','Overdue'). No date filter.

    Q: how many overdue invoices does Krishna Enterprises have
    ACTION: invoice side is ambiguous. Use run_sql twice.
    Sales SQL: SELECT COUNT(*) AS `overdue_count`
               FROM `tabSales Invoice`
               WHERE `docstatus` = 1
               AND `customer` LIKE '%Krishna Enterprises%'
               AND `status` = 'Overdue'
    Purchase SQL: SELECT COUNT(*) AS `overdue_count`
                  FROM `tabPurchase Invoice`
                  WHERE `docstatus` = 1
                  AND `supplier` LIKE '%Krishna Enterprises%'
                  AND `status` = 'Overdue'
    NOTE: Report both counts, or the combined total if the user asked broadly.
    NOTE: Do NOT search only tabSales Invoice for this question.

    Q: how many invoices does Shadab Ahmer have by status
    ACTION: invoice side is ambiguous and cancelled invoices are requested/needed.
            Use run_sql twice with docstatus IN (1, 2).
    Sales SQL: SELECT COUNT(*) AS `invoice_count`,
                      SUM(CASE WHEN `docstatus` = 1 AND `status` = 'Unpaid' THEN 1 ELSE 0 END) AS `unpaid_count`,
                      SUM(CASE WHEN `docstatus` = 1 AND `status` = 'Overdue' THEN 1 ELSE 0 END) AS `overdue_count`,
                      SUM(CASE WHEN `docstatus` = 2 OR `status` = 'Cancelled' THEN 1 ELSE 0 END) AS `cancelled_count`
               FROM `tabSales Invoice`
               WHERE `docstatus` IN (1, 2)
               AND `customer` LIKE '%Shadab Ahmer%'
    Purchase SQL: SELECT COUNT(*) AS `invoice_count`,
                         SUM(CASE WHEN `docstatus` = 1 AND `status` = 'Unpaid' THEN 1 ELSE 0 END) AS `unpaid_count`,
                         SUM(CASE WHEN `docstatus` = 1 AND `status` = 'Overdue' THEN 1 ELSE 0 END) AS `overdue_count`,
                         SUM(CASE WHEN `docstatus` = 2 OR `status` = 'Cancelled' THEN 1 ELSE 0 END) AS `cancelled_count`
                  FROM `tabPurchase Invoice`
                  WHERE `docstatus` IN (1, 2)
                  AND `supplier` LIKE '%Shadab Ahmer%'

    Q: who has the most unpaid invoices
    ACTION: invoice side is ambiguous and this is a ranking question.
            Use run_sql twice.
    Sales SQL: SELECT `customer`, COUNT(*) AS `unpaid_count`,
                      SUM(`outstanding_amount`) AS `total_outstanding`
               FROM `tabSales Invoice`
               WHERE `docstatus` = 1 AND `status` = 'Unpaid'
               GROUP BY `customer`
               ORDER BY `unpaid_count` DESC, `total_outstanding` DESC
               LIMIT 1
    Purchase SQL: SELECT `supplier`, COUNT(*) AS `unpaid_count`,
                         SUM(`outstanding_amount`) AS `total_outstanding`
                  FROM `tabPurchase Invoice`
                  WHERE `docstatus` = 1 AND `status` = 'Unpaid'
                  GROUP BY `supplier`
                  ORDER BY `unpaid_count` DESC, `total_outstanding` DESC
                  LIMIT 1
    NOTE: Compare the Sales and Purchase top rows and report both if helpful.

    Q: how many overdue purchase invoices does Hettich India Private Limited have
    SQL: SELECT COUNT(*) AS `overdue_count`
         FROM `tabPurchase Invoice`
         WHERE `docstatus` = 1
         AND `supplier` LIKE '%Hettich India%'
         AND `status` = 'Overdue'
    NOTE: No date filter. Counts ALL overdue invoices regardless of date.

    Q: how many unpaid purchase invoices does supplier X have
    SQL: SELECT COUNT(*) AS `unpaid_count`
        FROM `tabPurchase Invoice`
        WHERE `docstatus` = 1
        AND `supplier` LIKE '%X%'
        AND `status` = 'Unpaid'
    NOTE: Use status = 'Unpaid' ONLY. Do NOT include Overdue unless user explicitly asks for both.

    Q: total outstanding amount owed to supplier X
    SQL: SELECT SUM(`outstanding_amount`) AS `total_outstanding`
         FROM `tabPurchase Invoice`
         WHERE `docstatus` = 1
         AND `supplier` LIKE '%X%'
         AND `outstanding_amount` > 0

    Q: how much tax did Golden Twist pay
    SQL: SELECT SUM(`total_taxes_and_charges`) AS `total_tax`
         FROM `tabSales Invoice`
         WHERE `docstatus` = 1
         AND `customer` LIKE '%Golden Twist%'

    Q: which supplier do i owe most money to
    SQL: SELECT `supplier`, SUM(`outstanding_amount`) AS `total_due`
         FROM `tabPurchase Invoice`
         WHERE `docstatus` = 1 AND `outstanding_amount` > 0
         GROUP BY `supplier`
         ORDER BY `total_due` DESC LIMIT 1

    Q: top 5 customers by revenue
    SQL: SELECT `customer`, SUM(`grand_total`) AS `total_revenue`
         FROM `tabSales Invoice`
         WHERE `docstatus` = 1
         GROUP BY `customer`
         ORDER BY `total_revenue` DESC LIMIT 5

    Q: total purchases this month
    SQL: SELECT SUM(`grand_total`) AS `total`
         FROM `tabPurchase Invoice`
         WHERE `docstatus` = 1
         AND MONTH(`posting_date`) = MONTH(CURDATE())
         AND YEAR(`posting_date`) = YEAR(CURDATE())

    Q: most sold item
    SQL: SELECT i.`item_code`, i.`item_name`, SUM(i.`qty`) AS `total_qty`
        FROM `tabSales Invoice Item` i
        JOIN `tabSales Invoice` s ON s.`name` = i.`parent`
        WHERE i.`docstatus` = 1 AND s.`docstatus` = 1
        GROUP BY i.`item_code`, i.`item_name`
        ORDER BY `total_qty` DESC LIMIT 1

    Q: list active employees
    SQL: SELECT `employee_name`, `designation`, `department`
         FROM `tabEmployee`
         WHERE `status` = 'Active'
         ORDER BY `employee_name` ASC
    ================================================================

    If run_sql returns NO_RESULTS for a named entity, immediately call
    search_entity with that name â€” it checks both sales and purchase tables.
    """
    rows, err = _execute(query)
    if err:
        return f"ERROR: {err}"
    result = _fmt(rows)
    print(f"  [run_sql] rows={len(rows)} | sql={query[:120]}")
    return result


# ---------------------------------------------------------------------------
# TOOL 3 â€” search_entity (dual-table retry)
# ---------------------------------------------------------------------------

@mcp.tool()
def search_entity(name: str) -> str:
    """
    Search a name in BOTH tabSales Invoice (as customer) AND
    tabPurchase Invoice (as supplier) simultaneously.

    Use this tool when:
    1. You are unsure if a name is a customer or supplier
    2. run_sql returned NO_RESULTS for a name-based query
    3. The user asks about an entity without specifying customer/supplier
    4. The user provides an invoice ID like SINV-XXXX or PINV-XXXX

    This is the automatic dual-table retry â€” always call this before
    giving up on a name-based question.

    Returns invoice count, status breakdown, total billed, outstanding,
    and tax for the entity in both sales and purchase tables.

    Entity summaries include submitted and cancelled invoices:
    docstatus IN (1, 2). Draft invoices are excluded.
    """
    safe_name = str(name).replace("\\", "\\\\").replace("'", "''")

    # ------------------------------------------------------------------
    # INVOICE ID DETECTION â€” handle SINV-XXXX / PINV-XXXX / INV-XXXX
    # ------------------------------------------------------------------
    invoice_prefixes = ("SINV-", "PINV-", "INV-", "ACC-SINV-", "ACC-PINV-")
    if any(name.upper().startswith(p) for p in invoice_prefixes):
        # Try Sales Invoice first
        s_rows, _ = _execute(f"""
            SELECT `name`, `customer`, `posting_date`, `due_date`,
                   `grand_total`, `outstanding_amount`, `status`, `docstatus`
            FROM `tabSales Invoice`
            WHERE `docstatus` IN (1, 2) AND `name` = '{safe_name}'
        """)
        if s_rows:
            return f"Sales Invoice '{name}':\n" + _fmt(s_rows)

        # Try Purchase Invoice
        p_rows, _ = _execute(f"""
            SELECT `name`, `supplier`, `posting_date`, `due_date`,
                   `grand_total`, `outstanding_amount`, `status`, `docstatus`
            FROM `tabPurchase Invoice`
            WHERE `docstatus` IN (1, 2) AND `name` = '{safe_name}'
        """)
        if p_rows:
            return f"Purchase Invoice '{name}':\n" + _fmt(p_rows)

        return f"NO_RESULTS: Invoice '{name}' not found in Sales or Purchase invoices."

    # ------------------------------------------------------------------
    # ENTITY NAME SEARCH â€” customer / supplier lookup
    # ------------------------------------------------------------------
    results = []

    def summarize_role(label: str, table: str, party_col: str, billed_label: str, due_label: str):
        rows, _ = _execute(f"""
            SELECT COUNT(*) AS `invoice_count`,
                   SUM(CASE WHEN `docstatus` = 1 AND `status` = 'Unpaid' THEN 1 ELSE 0 END) AS `unpaid_count`,
                   SUM(CASE WHEN `docstatus` = 1 AND `status` = 'Overdue' THEN 1 ELSE 0 END) AS `overdue_count`,
                   SUM(CASE WHEN `docstatus` = 2 OR `status` = 'Cancelled' THEN 1 ELSE 0 END) AS `cancelled_count`,
                   SUM(CASE WHEN `docstatus` = 1 AND `status` = 'Paid' THEN 1 ELSE 0 END) AS `paid_count`,
                   SUM(CASE
                         WHEN `docstatus` = 1
                          AND `status` NOT IN ('Unpaid', 'Overdue', 'Paid', 'Cancelled')
                         THEN 1 ELSE 0
                       END) AS `other_count`,
                   SUM(CASE WHEN `docstatus` = 1 THEN `grand_total` ELSE 0 END) AS `total_billed`,
                   SUM(CASE WHEN `docstatus` = 1 THEN `outstanding_amount` ELSE 0 END) AS `total_outstanding`,
                   SUM(CASE WHEN `docstatus` = 1 THEN `total_taxes_and_charges` ELSE 0 END) AS `total_tax`
            FROM `{table}`
            WHERE `docstatus` IN (1, 2) AND `{party_col}` LIKE '%{safe_name}%'
        """)
        r = rows[0] if rows else {}
        count = int(r.get("invoice_count") or 0)
        parts = [
            f"Found as {label}",
            f"Invoices: {count}",
            f"Unpaid: {int(r.get('unpaid_count') or 0)}",
            f"Overdue: {int(r.get('overdue_count') or 0)}",
            f"Cancelled: {int(r.get('cancelled_count') or 0)}",
        ]
        paid_count = int(r.get("paid_count") or 0)
        other_count = int(r.get("other_count") or 0)
        if paid_count:
            parts.append(f"Paid: {paid_count}")
        if other_count:
            parts.append(f"Other submitted statuses: {other_count}")
        if r.get("total_billed"):
            parts.append(f"{billed_label} (submitted only): â‚¹{r['total_billed']:,.2f}")
        if r.get("total_outstanding") and r["total_outstanding"] > 0:
            parts.append(f"{due_label} (submitted only): â‚¹{r['total_outstanding']:,.2f}")
        if r.get("total_tax") and r["total_tax"] > 0:
            parts.append(f"Tax (submitted only): â‚¹{r['total_tax']:,.2f}")
        return count, " | ".join(parts)

    customer_count, customer_line = summarize_role(
        "CUSTOMER", "tabSales Invoice", "customer", "Total Billed", "Outstanding"
    )
    supplier_count, supplier_line = summarize_role(
        "SUPPLIER", "tabPurchase Invoice", "supplier", "Total Purchased", "Payable"
    )

    if customer_count or supplier_count:
        results.append(customer_line)
        results.append(supplier_line)

    if not results:
        return f"NO_RESULTS: '{name}' not found in Sales or Purchase invoices."
    return f"Entity search for '{name}':\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# TOOL 4 â€” get_summary (pre-built dashboard)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_summary() -> str:
    """
    Returns a full business dashboard with real DB data. One call, no SQL needed.

    Use when user asks:
    'how is my business doing', 'give me a summary', 'business overview',
    'dashboard', 'financial summary', 'how am i doing'

    Returns: sales/purchases this month and year, unpaid invoice counts,
    total receivable, total payable, top customer, top supplier.
    """
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
        cur  = conn.cursor()
        lines = ["Business Summary\n" + "=" * 40]
        for label, q in queries.items():
            try:
                cur.execute(q)
                row = cur.fetchone()
                val = list(row.values())[0] if row else None
                if val is None:
                    lines.append(f"  {label}: No data")
                elif isinstance(val, float):
                    lines.append(f"  {label}: â‚¹{val:,.2f}")
                else:
                    lines.append(f"  {label}: {val}")
            except Exception as e:
                lines.append(f"  {label}: Error â€” {e}")
        conn.close()
        return "\n".join(lines)
    except Exception as e:
        return f"Connection error: {e}"


# ---------------------------------------------------------------------------
# TOOL 5 â€” get_top (pre-built ranking)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_top(entity_type: str = "customers", metric: str = "revenue", top_n: int = 5) -> str:
    """
    Returns top customers or suppliers ranked by a metric. No SQL needed.

    entity_type : 'customers' or 'suppliers'
    metric      : 'revenue'     â†’ ranked by total grand_total
                  'outstanding' â†’ ranked by total unpaid amount
    top_n       : how many to return (default 5)

    Use when user asks:
    'top customers', 'best suppliers', 'top 10 customers by revenue',
    'which supplier do i buy most from', 'who owes me most'
    """
    if entity_type == "customers":
        table, col = "tabSales Invoice", "customer"
    else:
        table, col = "tabPurchase Invoice", "supplier"

    if metric == "outstanding":
        agg, label, extra = "outstanding_amount", "total_outstanding", "AND `outstanding_amount`>0"
    else:
        agg, label, extra = "grand_total", "total_revenue", ""

    rows, err = _execute(f"""
        SELECT `{col}`, SUM(`{agg}`) AS `{label}`
        FROM `{table}` WHERE `docstatus`=1 {extra}
        GROUP BY `{col}` ORDER BY `{label}` DESC LIMIT {top_n}
    """)
    return err if err else _fmt(rows)


# ---------------------------------------------------------------------------
# TOOL 6 â€” get_trend (pre-built monthly trend)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_trend(trend_type: str = "sales", months: int = 6) -> str:
    """
    Returns monthly sales or purchase totals for the last N months. No SQL needed.

    trend_type : 'sales' or 'purchases'
    months     : how many months back (default 6)

    Use when user asks:
    'monthly trend', 'sales by month', 'which month had highest sales',
    'last 6 months performance', 'monthly purchases'
    """
    table = "tabSales Invoice" if trend_type == "sales" else "tabPurchase Invoice"
    rows, err = _execute(f"""
        SELECT YEAR(`posting_date`) AS `year`,
               MONTH(`posting_date`) AS `month`,
               SUM(`grand_total`) AS `total`
        FROM `{table}` WHERE `docstatus`=1
        AND `posting_date` >= DATE_SUB(CURDATE(), INTERVAL {months} MONTH)
        GROUP BY YEAR(`posting_date`), MONTH(`posting_date`)
        ORDER BY `year` ASC, `month` ASC
    """)
    return err if err else _fmt(rows)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("ERPNext MCP Server starting (stdio)...")
    mcp.run(transport="stdio")







