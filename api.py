# from fastapi import FastAPI
# from pydantic import BaseModel
# import pymysql
# import ollama
# import re
# import time
# from config import DB_CONFIG, LLM_MODEL, OLLAMA_BASE_URL

# app = FastAPI()
# client = ollama.Client(host=OLLAMA_BASE_URL)


# class ChatRequest(BaseModel):
#     model: str
#     messages: list



# def get_connection(retries=3):
#     for attempt in range(retries):
#         try:
#             return pymysql.connect(
#                 **DB_CONFIG,
#                 cursorclass=pymysql.cursors.DictCursor,
#                 connect_timeout=10,
#                 read_timeout=30,
#                 write_timeout=30,
#             )
#         except Exception as e:
#             print(f"  [Connection attempt {attempt+1} failed: {e}]")
#             time.sleep(2)
#     raise Exception("Could not connect to MariaDB.")



# ALLOWED_TABLES = [
#     "tabPurchase Invoice",
#     "tabSales Invoice",
#     "tabSupplier",
#     "tabCustomer",
#     "tabItem",
#     "tabEmployee",
#     "tabPayment Entry",
#     "tabJournal Entry",
#     "tabStock Entry",
# ]



# def get_schema(tables: list) -> str:
#     conn = get_connection()
#     schema = []
#     try:
#         cursor = conn.cursor()
#         for table in tables:
#             try:
#                 cursor.execute(f"SHOW COLUMNS FROM `{table}`")
#                 cols = [row["Field"] for row in cursor.fetchall()]
#                 schema.append(f"Table `{table}` columns: {', '.join(cols)}")
#             except Exception:
#                 pass
#         return "\n".join(schema)
#     finally:
#         conn.close()



# def fix_table_names(sql: str) -> str:
#     replacements = {
#         r'\btabSalesInvoice\b':                '`tabSales Invoice`',
#         r'\btabPurchaseInvoice\b':             '`tabPurchase Invoice`',
#         r'\btabPaymentEntry\b':                '`tabPayment Entry`',
#         r'\btabJournalEntry\b':                '`tabJournal Entry`',
#         r'\btabStockEntry\b':                  '`tabStock Entry`',
#         r'(?<!`)tabSales Invoice(?!`)':        '`tabSales Invoice`',
#         r'(?<!`)tabPurchase Invoice(?!`)':     '`tabPurchase Invoice`',
#         r'(?<!`)tabPayment Entry(?!`)':        '`tabPayment Entry`',
#         r'(?<!`)tabJournal Entry(?!`)':        '`tabJournal Entry`',
#         r'(?<!`)tabStock Entry(?!`)':          '`tabStock Entry`',
#         r'(?<!`)tabSupplier(?!`)':             '`tabSupplier`',
#         r'(?<!`)tabCustomer(?!`)':             '`tabCustomer`',
#         r'(?<!`)tabItem(?!`)':                 '`tabItem`',
#         r'(?<!`)tabEmployee(?!`)':             '`tabEmployee`',
#     }
#     for pattern, replacement in replacements.items():
#         sql = re.sub(pattern, replacement, sql)
#     return sql



# def extract_sql(raw: str) -> str:
#     """
#     Safely pull a SELECT statement out of Qwen3's noisy response.

#     Priority order:
#       1. Content inside ```sql ... ``` fences
#       2. SELECT ... up to the first blank line  (notes come after a blank line)
#       3. SELECT ... to end of string (last resort)

#     We deliberately do NOT strip bullet/dash lines before extraction because
#     those patterns appear inside SQL too (e.g. column lists with dashes in names).
#     """
#     print(f"  [Raw LLM output]:\n{raw}\n")

#     # Step 1: strip <think> blocks
#     raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

#     # Step 2: extract from markdown code fence if present
#     fenced = re.search(r'```(?:sql)?\s*(SELECT\b.+?)```', raw, re.IGNORECASE | re.DOTALL)
#     if fenced:
#         sql = fenced.group(1).strip().rstrip(';').strip()
#         print(f"  [Extracted from fence]: {sql}")
#         return sql

#     # Step 3: SELECT up to first blank line
#     # Qwen3 pattern: SQL block, then \n\n, then explanation
#     select_to_blank = re.search(
#         r'(SELECT\b.+?)(?=\n\s*\n)',
#         raw,
#         re.IGNORECASE | re.DOTALL,
#     )
#     if select_to_blank:
#         sql = select_to_blank.group(1).strip().rstrip(';').strip()
#         print(f"  [Extracted via blank-line boundary]: {sql}")
#         return sql

#     # Step 4: SELECT to end of string (fallback)
#     fallback = re.search(r'(SELECT\b.+)', raw, re.IGNORECASE | re.DOTALL)
#     if fallback:
#         sql = fallback.group(1).strip().rstrip(';').strip()
#         print(f"  [Extracted via fallback]: {sql}")
#         return sql

#     print("  [Could not extract SQL]")
#     return ""



# def generate_sql(question: str, schema: str) -> str:
#     table_list = "\n".join(f"  - `{t}`" for t in ALLOWED_TABLES)

#     prompt = f"""You are a MariaDB SQL expert.
# Output ONLY a raw SQL SELECT statement — nothing else.
# No markdown, no code fences, no explanation, no notes, no headers.
# Just the SQL query ending with a semicolon.

# RULES:
# 1. Wrap ALL table names in backticks exactly as listed below.
# 2. Wrap ALL column names in backticks.
# 3. Use `supplier` column in `tabPurchase Invoice` for the supplier name.
# 4. Use `customer` column in `tabSales Invoice` for the customer name.
# 5. For overdue invoices: `due_date` < CURDATE() AND `status` != 'Paid'
# 6. For status checks use `status` column directly (values: Paid, Unpaid, Overdue, Cancelled).
# 7. For oldest record: ORDER BY `due_date` ASC LIMIT 1
# 8. For newest record: ORDER BY `due_date` DESC LIMIT 1
# 9. Do NOT output anything after the semicolon.
# 10. Do NOT add any explanation or notes.
# 11. NEVER use UNION or UNION ALL — query only ONE table per statement.
# 12. When the question says "invoices" without specifying purchase or sales, use `tabSales Invoice`.
# 13. To get unpaid/outstanding amount use `outstanding_amount` column directly from `tabSales Invoice` or `tabPurchase Invoice`. Never calculate it manually.
# 14. Never JOIN `tabPayment Entry` to calculate outstanding amounts — use `outstanding_amount` instead.
# 15. For total unpaid amount: SELECT SUM(`outstanding_amount`) FROM `tabSales Invoice` WHERE `customer` = '...' AND `status` != 'Paid'

# Available tables (use EXACTLY these names):
# {table_list}

# Schema:
# {schema}

# Question: {question}

# SQL:"""

#     response = client.chat(
#         model=LLM_MODEL,
#         messages=[{"role": "user", "content": prompt}],
#         options={"temperature": 0},
#     )

#     raw = response["message"]["content"].strip()

#     # Extract clean SQL
#     sql = extract_sql(raw)

#     if not sql:
#         return ""

#     # Fix table name typos
#     sql = fix_table_names(sql)

#     # Remove LIMIT only when question is NOT about oldest/newest/first/last/top N
#     limiting_keywords = ["oldest", "newest", "latest", "first", "last", "top", "recent"]
#     if not any(w in question.lower() for w in limiting_keywords):
#         sql = re.sub(r'\bLIMIT\s+\d+\b', '', sql, flags=re.IGNORECASE).strip()

#     # Collapse all whitespace to single spaces
#     sql = re.sub(r'\s+', ' ', sql).strip()
#     sql = sql.rstrip(';').strip()

#     print(f"  [Final SQL]: {sql}")
#     return sql



# def validate_sql(sql: str) -> None:
#     quoted = re.findall(r'`([^`]+)`', sql)
#     used_tables = [q for q in quoted if q.lower().startswith('tab')]
#     for t in used_tables:
#         if t not in ALLOWED_TABLES:
#             raise Exception(
#                 f"Table `{t}` is not in the allowed list.\n"
#                 f"Allowed: {ALLOWED_TABLES}\nSQL: {sql}"
#             )



# def run_sql(sql: str) -> list:
#     if not sql.strip().upper().startswith("SELECT"):
#         raise Exception("Only SELECT queries are allowed.")

#     validate_sql(sql)

#     conn = get_connection()
#     try:
#         cursor = conn.cursor()
#         cursor.execute(sql)
#         rows = cursor.fetchall()
#         print(f"  [Row count]: {len(rows)}")
#         return rows
#     finally:
#         conn.close()



# def format_results(rows: list) -> str:
#     if not rows:
#         return "No results found."
#     result = []
#     for row in rows:
#         fields = ", ".join(
#             f"{k}: {v}" for k, v in row.items() if v is not None and v != ""
#         )
#         result.append(fields)
#     return "\n".join(result)



# SKIP_FIELDS = {
#     "creation", "modified", "modified_by", "owner", "docstatus",
#     "naming_series", "idx", "posting_time", "set_posting_time",
#     "update_outstanding_for_self", "update_billed_amount_in_purchase_receipt",
#     "conversion_rate", "buying_price_list", "price_list_currency",
#     "plc_conversion_rate", "base_total", "base_net_total",
#     "base_taxes_and_charges_added", "base_total_taxes_and_charges",
#     "base_grand_total", "base_rounding_adjustment", "base_rounded_total",
#     "base_in_words", "tax_withholding_net_total", "base_tax_withholding_net_total",
#     "taxes_and_charges_added", "apply_discount_on", "other_charges_calculation",
#     "party_account_currency", "is_opening", "language", "supplier_group",
#     "taxes_and_charges", "price_list_currency",
#     "update_billed_amount_in_purchase_receipt", "update_outstanding_for_self",
#     "tally_guid", "tally_voucher_type", "tally_voucher_number",
#     "gst_vehicle_type", "itc_classification", "gst_category",
#     "mode_of_transport", "lr_date", "against_expense_account",
#     "supplier_name", "title", "payment_terms_template",
# }


# def format_results_as_answer(rows: list) -> str:
#     if not rows:
#         return "No results found."

#     total = len(rows)
#     lines = [f"Found {total} result(s):\n"]

#     for i, row in enumerate(rows, 1):
#         lines.append(f"--- {i} ---")
#         for k, v in row.items():
#             if k in SKIP_FIELDS:
#                 continue
#             if v is None or v == "" or v == 0:
#                 continue
#             if isinstance(v, float):
#                 v = f"₹{v:,.2f}"
#             lines.append(f"  {k}: {v}")
#         lines.append("")

#     return "\n".join(lines)



# def answer_question(question: str) -> str:
#     # 1. Get schema
#     schema = get_schema(ALLOWED_TABLES)

#     # 2. Generate SQL
#     sql = generate_sql(question, schema)

#     if not sql or not sql.strip().upper().startswith("SELECT"):
#         return (
#             "Could not generate a valid SQL query for your question.\n"
#             f"Extracted output was: '{sql}'\n"
#             "Please rephrase your question."
#         )


#     try:
#         rows = run_sql(sql)
#     except Exception as e:
#         return f"SQL error: {e}\nGenerated SQL was: {sql}"

#     if not rows:
#         return "No results found for your query."


#     listing_keywords = ["all", "list", "show", "give", "every", "details", "detail"]
#     if any(w in question.lower() for w in listing_keywords):
#         return format_results_as_answer(rows)

    
#     db_result = format_results(rows)

#     summary_prompt = f"""You are a helpful ERP assistant. Answer the user's question using only the data below.

# User question: {question}

# Database returned {len(rows)} result(s):
# {db_result}

# Rules:
# - Use ALL results. Do not skip any.
# - Show amounts in INR (₹).
# - Show dates in readable format like "June 5, 2025".
# - Be concise and factual.
# - No markdown, no bullet points, no headers.

# Answer:"""

#     response = client.chat(
#         model=LLM_MODEL,
#         messages=[{"role": "user", "content": summary_prompt}],
#         options={"temperature": 0},
#     )
#     answer = response["message"]["content"]
#     answer = re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()
#     return answer



# @app.get("/")
# async def root():
#     return {"status": "ERPNext Text-to-SQL API is running"}


# @app.post("/v1/chat/completions")
# async def chat(request: ChatRequest):
#     question = next(
#         (m["content"] for m in reversed(request.messages) if m["role"] == "user"),
#         ""
#     )
#     try:
#         answer = answer_question(question)
#     except Exception as e:
#         answer = f"Error: {e}"

#     return {
#         "id": "erpnext-rag",
#         "object": "chat.completion",
#         "model": request.model,
#         "choices": [{
#             "index": 0,
#             "message": {
#                 "role": "assistant",
#                 "content": answer,
#             },
#             "finish_reason": "stop",
#         }],
#     }


# @app.get("/v1/models")
# async def models():
#     return {
#         "object": "list",
#         "data": [{
#             "id": "erpnext-invoices",
#             "object": "model",
#             "owned_by": "local",
#         }],
#     }