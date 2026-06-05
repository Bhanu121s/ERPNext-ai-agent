
import json
import os
import numpy as np

from sentence_transformers import SentenceTransformer
import faiss
import chromadb
from chromadb.config import Settings

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

EXAMPLES_FILE   = os.path.join(os.path.dirname(__file__), "examples.json")
CHROMA_DB_PATH  = os.path.join(os.path.dirname(__file__), "chroma_store")
COLLECTION_NAME = "erpnext_examples"
EMBED_MODEL     = "all-MiniLM-L6-v2"   # 90MB, runs fully locally, very fast
TOP_K           = 4                     # number of examples to retrieve


# ---------------------------------------------------------------------------
# SINGLETON STATE
# We load the model and build FAISS once per process, then cache.
# ---------------------------------------------------------------------------

_embedder: SentenceTransformer | None = None
_chroma_collection = None
_faiss_index: faiss.IndexFlatIP | None = None   # Inner Product = cosine on normalized vecs
_example_ids: list[str] = []                    # ordered list matching FAISS row indices
_examples_map: dict[str, dict] = {}             # id → {question, sql}


def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        print(f"  [RAG] Loading embedding model: {EMBED_MODEL}")
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def _get_chroma_collection():
    global _chroma_collection
    if _chroma_collection is None:
        client = chromadb.PersistentClient(
            path=CHROMA_DB_PATH,
            settings=Settings(anonymized_telemetry=False),
        )
        _chroma_collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        print(f"  [RAG] ChromaDB collection '{COLLECTION_NAME}' ready "
              f"({_chroma_collection.count()} docs stored)")
    return _chroma_collection


# ---------------------------------------------------------------------------
# BUILD / SYNC
# ---------------------------------------------------------------------------

def _load_examples_json() -> list[dict]:
    with open(EXAMPLES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _sync_chroma(examples: list[dict]) -> None:
    """
    Insert any examples that are not yet in ChromaDB.
    Uses question text as the stable ID so re-runs are idempotent.
    """
    collection = _get_chroma_collection()
    embedder   = _get_embedder()

    existing_ids = set(collection.get()["ids"])
    to_add = [ex for ex in examples
              if ex["question"] not in existing_ids]

    if not to_add:
        print(f"  [RAG] All {len(examples)} examples already in ChromaDB — skipping embed")
        return

    print(f"  [RAG] Embedding {len(to_add)} new examples → ChromaDB …")
    questions  = [ex["question"] for ex in to_add]
    embeddings = embedder.encode(questions, normalize_embeddings=True).tolist()

    collection.add(
        ids        = questions,                       # question text = unique ID
        embeddings = embeddings,
        documents  = questions,
        metadatas  = [{"sql": ex["sql"]} for ex in to_add],
    )
    print(f"  [RAG] ChromaDB now has {collection.count()} examples")


def _build_faiss() -> None:
    """
    Pull all vectors from ChromaDB and build a FAISS IndexFlatIP.
    Inner Product on L2-normalized vectors == cosine similarity.
    This is rebuilt once per process (fast — just a memcpy).
    """
    global _faiss_index, _example_ids, _examples_map

    collection = _get_chroma_collection()
    result     = collection.get(include=["embeddings", "documents", "metadatas"])

    if not result["ids"]:
        print("  [RAG] ChromaDB is empty — no FAISS index built")
        return

    ids        = result["ids"]
    embeddings = np.array(result["embeddings"], dtype="float32")
    dim        = embeddings.shape[1]

    _faiss_index  = faiss.IndexFlatIP(dim)
    _faiss_index.add(embeddings)

    _example_ids  = ids
    _examples_map = {
        doc: {"question": doc, "sql": meta["sql"]}
        for doc, meta in zip(result["documents"], result["metadatas"])
    }

    print(f"  [RAG] FAISS index built: {_faiss_index.ntotal} vectors, dim={dim}")


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------

def initialize_rag() -> None:
    """
    Call once at startup (e.g. in FastAPI lifespan or module-level).
    Loads examples.json → syncs to ChromaDB → builds FAISS index.
    """
    examples = _load_examples_json()
    _sync_chroma(examples)
    _build_faiss()


def get_relevant_examples(question: str, top_k: int = TOP_K) -> str:
    """
    Returns a formatted string of the top-K most semantically similar
    examples to inject into the LLM prompt.
    Falls back to word-overlap if RAG is not initialized.
    """
    global _faiss_index

    # --- lazy init if not already done ---
    if _faiss_index is None:
        print("  [RAG] Index not ready — initializing now")
        initialize_rag()

    if _faiss_index is None or _faiss_index.ntotal == 0:
        return _word_overlap_fallback(question, top_k)

    embedder = _get_embedder()
    q_vec    = embedder.encode([question], normalize_embeddings=True).astype("float32")

    k        = min(top_k, _faiss_index.ntotal)
    scores, indices = _faiss_index.search(q_vec, k)

    top_examples = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        ex_id = _example_ids[idx]
        ex    = _examples_map.get(ex_id)
        if ex:
            top_examples.append((float(score), ex))

    print(f"  [RAG] Top-{k} scores: {[round(s,3) for s,_ in top_examples]}")

    if not top_examples:
        return _word_overlap_fallback(question, top_k)

    return _format_examples(top_examples)


def add_example(question: str, sql: str) -> None:
    """
    Add a new example at runtime (e.g. from an admin endpoint).
    Updates ChromaDB + rebuilds FAISS index.
    """
    collection = _get_chroma_collection()
    embedder   = _get_embedder()

    embedding = embedder.encode([question], normalize_embeddings=True).tolist()
    collection.upsert(
        ids        = [question],
        embeddings = embedding,
        documents  = [question],
        metadatas  = [{"sql": sql}],
    )

    # also write to examples.json for persistence
    examples = _load_examples_json()
    existing_qs = {ex["question"] for ex in examples}
    if question not in existing_qs:
        examples.append({"question": question, "sql": sql})
        with open(EXAMPLES_FILE, "w", encoding="utf-8") as f:
            json.dump(examples, f, indent=2, ensure_ascii=False)
        print(f"  [RAG] Added to examples.json: {question[:60]}")

    _build_faiss()  # rebuild index with new vector
    print(f"  [RAG] New example added + index rebuilt")


# ---------------------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------------------

def _format_examples(scored: list[tuple[float, dict]]) -> str:
    lines = ["--- RELEVANT EXAMPLES (learn the pattern) ---"]
    for score, ex in scored:
        lines.append(f"Q: {ex['question']}")
        lines.append(f"A: {ex['sql']}\n")
    lines.append("--- END OF EXAMPLES ---")
    return "\n".join(lines)


def _word_overlap_fallback(question: str, top_k: int) -> str:
    """Simple word-overlap fallback when RAG is unavailable."""
    import re
    stop = {"i","me","my","the","a","an","do","did","does","have","has","is",
            "are","was","were","to","for","of","in","on","at","how","what",
            "which","who","much","many","all","any","some","this","that"}

    def score(q1, q2):
        w1 = set(re.findall(r'\w+', q1.lower())) - stop
        w2 = set(re.findall(r'\w+', q2.lower())) - stop
        return len(w1 & w2) / len(w1 | w2) if w1 and w2 else 0.0

    try:
        examples = _load_examples_json()
    except Exception:
        return ""

    scored = sorted(examples, key=lambda ex: score(question, ex["question"]), reverse=True)
    top    = [ex for ex in scored[:top_k] if score(question, ex["question"]) > 0] or examples[:3]

    lines = ["--- RELEVANT EXAMPLES (learn the pattern) ---"]
    for ex in top:
        lines.append(f"Q: {ex['question']}")
        lines.append(f"A: {ex['sql']}\n")
    lines.append("--- END OF EXAMPLES ---")
    return "\n".join(lines)