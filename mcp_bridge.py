
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import ollama
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastmcp import Client as MCPClient
from fastmcp.client.transports import PythonStdioTransport
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

MCP_SERVER_PATH = str(Path(__file__).parent / "mcp_server.py")
LLM_MODEL       = "qwen2.5-coder:14b"
OLLAMA_HOST     = "http://localhost:11434"
MAX_TOOL_ROUNDS = 8

ollama_client = ollama.Client(host=OLLAMA_HOST)


def get_transport() -> PythonStdioTransport:
    """Fresh transport instance per call â€” required for stdio."""
    return PythonStdioTransport(MCP_SERVER_PATH)


# ---------------------------------------------------------------------------
# TOOL REGISTRY
# ---------------------------------------------------------------------------

_tool_definitions: list = []
_tool_names: set = set()


async def load_tools() -> list:
    """Load tool definitions from mcp_server.py at startup."""
    try:
        async with MCPClient(get_transport()) as mcp:
            tools_list = await mcp.list_tools()
            defs = []
            for tool in tools_list:
                defs.append({
                    "type": "function",
                    "function": {
                        "name":        tool.name,
                        "description": tool.description,
                        "parameters":  tool.inputSchema,
                    },
                })
            names = [d["function"]["name"] for d in defs]
            print(f"  [MCP] Loaded {len(defs)} tools: {names}")
            return defs
    except Exception as e:
        print(f"  [MCP] ERROR loading tools: {e}")
        print(f"  Make sure mcp_server.py is in the same folder as mcp_bridge.py")
        sys.exit(1)


async def call_tool(name: str, arguments: dict) -> str:
    """Execute one tool call on mcp_server.py via stdio."""
    try:
        async with MCPClient(get_transport()) as mcp:
            result = await mcp.call_tool(name, arguments)
            if isinstance(result, list):
                return "\n".join(
                    r.text if hasattr(r, "text") else str(r)
                    for r in result
                )
            return str(result)
    except Exception as e:
        return f"Tool error ({name}): {e}"


# ---------------------------------------------------------------------------
# SYSTEM PROMPT
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an ERPNext business intelligence assistant.
You have tools that query a live MariaDB database.

BEHAVIOUR RULES:
1. Always use tools to get real data. Never guess or invent numbers.
2. When run_sql returns NO_RESULTS for a name, immediately call
   search_entity â€” it checks both sales AND purchase tables.
3. NEVER add date filters unless the user mentions a date, month,
   year, or time period. "how many invoices does X have" = ALL records.
4. NEVER say "as of [date]" â€” data is live from the database.
5. NEVER say "I don't have information" if a tool can fetch it.
6. Show monetary values as â‚¹ with comma formatting.
7. Summarize tool results clearly in plain English.
8. When displaying ranked results (top N), always show ALL N rows.
   Never truncate, merge, or replace items with placeholders.

INVOICE ID vs ENTITY NAME:
- If user provides an invoice ID (starts with SINV-, PINV-, INV-),
  use run_sql with WHERE `name` = 'SINV-XXXX' or call search_entity.
  NEVER use LIKE for invoice IDs â€” use exact match.
- search_entity is for customer/supplier NAMES only (e.g. 'FMS Traders').
- For invoice IDs, prefer: 
  SELECT `due_date`,`status`,`outstanding_amount` 
  FROM `tabSales Invoice` WHERE `name` = 'SINV-XXXX' AND `docstatus`=1

TOOL GUIDE:
  run_sql        â†’ write and run a SELECT query
  search_entity  â†’ find a name in both customer + supplier tables
  get_schema     â†’ check exact column names before writing SQL
  get_summary    â†’ full business dashboard (no SQL needed)
  get_top        â†’ top customers/suppliers ranking (no SQL needed)
  get_trend      â†’ monthly sales/purchase trend (no SQL needed)
  

UNPAID / OVERDUE INVOICE RULES:
- Use run_sql for all unpaid, overdue, outstanding, receivable, and payable invoice questions.
  There is no separate unpaid tool.
- If user says only invoice/invoices and does not clearly say sales/customer or purchase/supplier,
  run two separate queries: tabSales Invoice by customer and tabPurchase Invoice by supplier.
  Then report both results or a combined answer if the user asked broadly.
- For unpaid use status = 'Unpaid'. For overdue use status = 'Overdue'.
- For unpaid or overdue, outstanding, receivable, or payable, use status IN ('Unpaid','Overdue')
  or outstanding_amount > 0 as appropriate.
- For who has most/top/highest unpaid invoices, use GROUP BY, COUNT(*), ORDER BY count DESC, and LIMIT.
  Do not list a few invoices and infer.
  NOTE: tabSales Invoice Item â€” use `qty` for unit quantities, `amount`
  for rupee value. SUM(qty) returns units sold, SUM(amount) returns revenue.
  """



# ---------------------------------------------------------------------------
# AGENTIC LOOP
# ---------------------------------------------------------------------------

def _format_final(tool_result: str, question: str) -> str:
    """Convert a raw tool result into a plain English answer."""
    if "NO_RESULTS" in tool_result:
        return "No matching records found in the database."
    # Strip the CallToolResult wrapper if present
    text_match = re.search(r"text='([^']+)'", tool_result)
    if text_match:
        tool_result = text_match.group(1).replace("\\n", "\n")
    return tool_result

async def answer_question(question: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": question},
    ]

    last_tool_result = ""
    seen_calls: set  = set()

    for round_num in range(MAX_TOOL_ROUNDS):
        print(f"\n  [Round {round_num + 1}] Sending to Ollama...")

        response   = ollama_client.chat(
            model    = LLM_MODEL,
            messages = messages,
            tools    = _tool_definitions,
            options  = {"temperature": 0, "num_ctx": 4096},
        )
        msg        = response["message"]
        tool_calls = msg.get("tool_calls", [])
        content    = msg.get("content", "").strip()
        content    = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        # Fallback: only on round 1 (no tools have run yet)
        if not tool_calls and content and round_num == 0:
            json_match = re.search(
                r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^}]+\})\s*\}',
                content, re.DOTALL
            )
            if json_match:
                try:
                    parsed_args = json.loads(json_match.group(2))
                    tool_calls = [{
                        "function": {
                            "name":      json_match.group(1),
                            "arguments": parsed_args,
                        }
                    }]
                    print(f"  [Fallback] Parsed raw JSON tool call: {json_match.group(1)}")
                    content = ""
                except Exception:
                    pass

        # No tool calls â†’ model gave final answer
        if not tool_calls:
            if last_tool_result:
                print(f"  [Done in round {round_num + 1}]")
                return _format_final(last_tool_result, question)
            if content and re.search(r'^\s*\{\s*"name"\s*:', content):
                print(f"  [Final content is raw JSON â€” no tool result]")
                return "No answer found."
            print(f"  [Done in round {round_num + 1}]")
            return content or "No answer found. Please rephrase your question."
        

        # Append assistant turn
        messages.append({
            "role":       "assistant",
            "content":    msg.get("content", ""),
            "tool_calls": tool_calls,
        })

        # Execute each tool call
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            tool_args = tc["function"].get("arguments", {})

            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except Exception:
                    tool_args = {}

            # Loop detection â€” same call twice = break
            call_key = (tool_name, json.dumps(tool_args, sort_keys=True))
            if call_key in seen_calls:
                print(f"  [Loop detected] {tool_name} repeated â€” returning last result")
                return _format_final(last_tool_result, question)
            seen_calls.add(call_key)

            print(f"  [Tool] {tool_name}({tool_args})")
            result = await call_tool(tool_name, tool_args)
            print(f"  [Result] {result[:200]}")

            last_tool_result = result
            messages.append({"role": "tool", "content": result})

    return _format_final(last_tool_result, question) if last_tool_result \
        else "Reached maximum tool rounds. Please try a simpler question."


# ---------------------------------------------------------------------------
# FASTAPI
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tool_definitions, _tool_names
    print("\n[Startup] Loading MCP tools...")
    _tool_definitions = await load_tools()
    _tool_names       = {t["function"]["name"] for t in _tool_definitions}
    print(f"[Startup] Ready â€” {len(_tool_definitions)} tools loaded.")
    print(f"[Startup] Open WebUI â†’ http://host.docker.internal:8000/v1")
    print(f"[Startup] Browser   â†’ http://localhost:8000\n")
    yield


app = FastAPI(title="ERPNext MCP Bridge", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    model: str
    messages: list


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    question = next(
        (m["content"] for m in reversed(request.messages) if m["role"] == "user"), ""
    )
    try:
        answer = await answer_question(question.strip())
    except Exception as e:
        answer = f"Error: {e}"

    return {
        "id":      "erpnext-mcp",
        "object":  "chat.completion",
        "model":   request.model,
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data":   [{"id": "erpnext-mcp", "object": "model", "owned_by": "local"}],
    }


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
async def ask(req: AskRequest):
    if not req.question.strip():
        return JSONResponse({"answer": "Please enter a question."})
    try:
        answer = await answer_question(req.question.strip())
    except Exception as e:
        answer = f"Error: {e}"
    return JSONResponse({"answer": answer})


@app.get("/health")
async def health():
    return {"status": "ok", "model": LLM_MODEL, "tools": list(_tool_names)}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "chat.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>chat.html not found â€” place it in the same folder as mcp_bridge.py</h1>"
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
