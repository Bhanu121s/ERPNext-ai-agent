# ERPNext AI Business Intelligence Agent

A local, natural-language business intelligence assistant for ERPNext. Ask questions like *"how much does FMS Traders owe us?"* or *"top 5 customers by revenue"* in plain English, over the browser or WhatsApp, and get a real answer pulled live from the ERPNext/MariaDB database — no dashboards, no manual SQL.

Built during a Software Engineer internship at SPLCG for a granite manufacturing company. The agent correctly answers ~96% of real business questions (customer dues, due dates, taxes, outstanding amounts, rankings, trends).

Runs entirely on a locally-hosted LLM (Ollama) — no cloud API dependency for the core query pipeline.

## Active files

The whole system runs on two files:

### `mcp_server.py`
A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes 7 tools for querying ERPNext data directly from MariaDB:

| Tool | Purpose |
|---|---|
| `get_schema` | Fetch live column names for a table before writing SQL |
| `run_sql` | Run a raw `SELECT` query |
| `search_entity` | Look up a name across both customer and supplier tables at once |
| `get_summary` | Pre-built business dashboard (no SQL needed) |
| `get_unpaid` | Pre-built unpaid/overdue invoice list |
| `get_top` | Pre-built top customers/suppliers ranking |
| `get_trend` | Pre-built monthly sales/purchase trend |

Every tool enforces safeguards baked into the query layer: only `SELECT` statements are allowed, table names are checked against a whitelist to prevent SQL injection, and `docstatus = 1` filtering is applied consistently so cancelled/draft ERPNext records never get counted.

### `mcp_bridge.py`
A FastAPI service that runs the actual agentic loop. It:
- Loads the tool definitions from `mcp_server.py` over an MCP stdio connection at startup
- Sends the user's question to a local Ollama model along with those tool definitions
- Lets the model call one or more tools (up to 8 rounds), detecting and breaking out of repeated/looping calls
- Formats the final tool result into a plain-English answer
- Exposes an OpenAI-compatible `/v1/chat/completions` endpoint (used by Open WebUI in the browser) and a simple `/ask` endpoint (used for WhatsApp delivery)

## How OpenClaw / Hermes fit in

OpenClaw (and now [Hermes Agent](https://github.com), which has replaced it) is a separate, terminal-based **MCP client**. It connects to `mcp_server.py`'s tools independently of `mcp_bridge.py` — same tool definitions, different consumption path. Where `mcp_bridge.py` handles the web/WhatsApp-facing agent loop via direct Ollama tool calling, OpenClaw/Hermes handles interactive terminal use, calling the same MCP tools to answer questions.

Both clients talk to one source of truth: the tools defined in `mcp_server.py`.

## Not in use

This repo also contains an earlier approaches such as direct API implementaition and RAG-based approach (semantic retrieval over a predefined/tagged question set using ChromaDB/FAISS embeddings). It's kept for reference but **not used** — it could only answer questions matching its trained question set and didn't generalize to arbitrary business questions. That limitation is exactly why the project moved to the current MCP tool-based architecture, where the model can reason over real schema and live data instead of matching against fixed examples.
