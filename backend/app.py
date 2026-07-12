"""
app.py — MetaLens (unified): a data catalog that combines a graph-based
lineage engine with LLM-powered SQL extraction and RAG question-answering,
all over ONE shared catalog.

Endpoints fall into two families that operate on the same LineageGraph:

  GRAPH (deterministic, no LLM needed):
    /api/datasets, /api/dataset/{id}, /api/lineage/{id}, /api/impact/{id},
    /api/path, /api/build-order, /api/graph

  AI (require GROQ_API_KEY):
    /api/ask                 — RAG over the catalog
    /api/extract-lineage     — SQL -> column lineage, can be added to the graph

The integration: lineage extracted from SQL is upserted into the SAME graph,
so a newly-extracted table immediately appears in the graph view, in impact
analysis, and in RAG answers.

Run:  uvicorn backend.app:app --reload --port 8000
"""

from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # GROQ_API_KEY from .env if present (never committed)

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm
from .lineage import Column, Dataset, LineageGraph
from . import rag
from .rag import VectorStore, Doc, embedding_backend
from .sql_lineage import extract_lineage, validate_against_known

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "data" / "catalog.yaml"
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="MetaLens", version="2.0.0",
              description="Graph lineage engine + LLM SQL extraction + RAG, over one catalog.")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

graph = LineageGraph()
store = VectorStore()


# ---------- catalog loading ----------
def load_catalog() -> None:
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    for entry in raw.get("datasets", []):
        columns = [
            Column(name=c["name"], type=c.get("type", "unknown"),
                   description=c.get("description", ""),
                   derived_from=c.get("derived_from", []) or [])
            for c in entry.get("columns", [])
        ]
        graph.add_dataset(Dataset(
            id=entry["id"], name=entry["name"], layer=entry["layer"],
            owner=entry["owner"], description=entry.get("description", ""),
            tags=entry.get("tags", []) or [], sources=entry.get("sources", []) or [],
            columns=columns,
        ))
    graph.build_edges()


def dataset_to_doc(ds: Dataset) -> Doc:
    """Turn a Dataset into a retrievable RAG document (lineage in words)."""
    cols = ", ".join(f"{c.name} ({c.type})" for c in ds.columns)
    lin = []
    for c in ds.columns:
        if c.derived_from:
            lin.append(f"  - {c.name} is derived from {', '.join(c.derived_from)}")
    lin_txt = "\n".join(lin) or "  (no column lineage recorded)"
    sources = graph.direct_sources(ds.id)
    targets = graph.direct_targets(ds.id)
    text = (
        f"Dataset: {ds.id} ({ds.name}).\n"
        f"Governance layer: {ds.layer}. Owner: {ds.owner}.\n"
        f"Description: {ds.description}\nTags: {', '.join(ds.tags)}.\n"
        f"Columns: {cols}.\n"
        f"Built from (sources): {', '.join(sources) if sources else 'none — raw source'}.\n"
        f"Feeds (targets): {', '.join(targets) if targets else 'none — final output'}.\n"
        f"Column-level lineage:\n{lin_txt}"
    )
    return Doc(id=ds.id, text=text,
               meta={"layer": ds.layer, "owner": ds.owner,
                     "sources": sources, "targets": targets})


def reindex() -> None:
    store.build([dataset_to_doc(ds) for ds in graph.all_datasets()])


@app.on_event("startup")
def _startup() -> None:
    load_catalog()
    reindex()                 # instant: uses the hashing embedder
    # Load the neural model in the background and re-index once it's ready.
    # This keeps startup fast so the platform's health probe always passes.
    rag.on_model_ready = reindex
    rag.start_model_load()


# ---------- serialization ----------
def _dataset_dict(ds: Dataset) -> dict:
    return {
        "id": ds.id, "name": ds.name, "layer": ds.layer, "owner": ds.owner,
        "description": ds.description, "tags": ds.tags,
        "columns": [{"name": c.name, "type": c.type, "description": c.description,
                     "derived_from": c.derived_from} for c in ds.columns],
        "sources": graph.direct_sources(ds.id),
        "targets": graph.direct_targets(ds.id),
    }


# ================= GRAPH ENDPOINTS =================

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "llm_configured": llm.is_configured(),
            "model": llm.DEFAULT_MODEL, "embedding_backend": embedding_backend(),
            "stats": graph.stats()}


@app.get("/api/datasets")
def list_datasets(q: str | None = None, layer: str | None = None) -> list[dict]:
    out = []
    for ds in graph.all_datasets():
        if layer and ds.layer != layer:
            continue
        if q and q.lower() not in f"{ds.id} {ds.name} {ds.owner} {' '.join(ds.tags)}".lower():
            continue
        out.append(_dataset_dict(ds))
    return sorted(out, key=lambda d: (d["layer"], d["id"]))


@app.get("/api/dataset/{dataset_id}")
def get_dataset(dataset_id: str) -> dict:
    ds = graph.get(dataset_id)
    if ds is None:
        raise HTTPException(404, f"Dataset '{dataset_id}' not found")
    return _dataset_dict(ds)


@app.get("/api/lineage/{dataset_id}")
def lineage(dataset_id: str) -> dict:
    if graph.get(dataset_id) is None:
        raise HTTPException(404, f"Dataset '{dataset_id}' not found")
    up = graph.upstream(dataset_id)
    down = graph.downstream(dataset_id)
    ids = set(up) | set(down) | {dataset_id}
    nodes = [{"id": n, "name": graph.get(n).name, "layer": graph.get(n).layer,
              "role": "focus" if n == dataset_id else ("upstream" if n in up else "downstream")}
             for n in ids]
    edges = [{"source": n, "target": t} for n in ids
             for t in graph.direct_targets(n) if t in ids]
    return {"focus": dataset_id, "upstream_count": len(up),
            "downstream_count": len(down), "nodes": nodes, "edges": edges}


@app.get("/api/impact/{dataset_id}")
def impact(dataset_id: str) -> dict:
    if graph.get(dataset_id) is None:
        raise HTTPException(404, f"Dataset '{dataset_id}' not found")
    return graph.impact_of(dataset_id)


@app.get("/api/path")
def path(src: str, dst: str) -> dict:
    try:
        p = graph.shortest_path(src, dst)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"src": src, "dst": dst, "path": p, "found": p is not None}


@app.get("/api/build-order")
def build_order() -> dict:
    try:
        return {"order": graph.topological_order(), "has_cycle": False}
    except ValueError as e:
        return {"order": [], "has_cycle": True, "detail": str(e)}


@app.get("/api/graph")
def full_graph() -> dict:
    nodes = [{"id": ds.id, "name": ds.name, "layer": ds.layer, "owner": ds.owner}
             for ds in graph.all_datasets()]
    edges = [{"source": ds.id, "target": t}
             for ds in graph.all_datasets() for t in graph.direct_targets(ds.id)]
    return {"nodes": nodes, "edges": edges, "stats": graph.stats()}


# ================= AI ENDPOINTS =================

class AskRequest(BaseModel):
    question: str
    k: int = 4


class ExtractRequest(BaseModel):
    sql: str
    add_to_catalog: bool = False


ANSWER_SYSTEM_PROMPT = """You are a data-catalog assistant. Answer the user's
question about the data warehouse using ONLY the context provided below, which
describes datasets, their columns, and their lineage.

Rules:
- Ground every claim in the context. If it isn't there, say so — do not invent
  tables, columns, or lineage.
- Cite datasets in square brackets, e.g. [mart.fct_orders].
- Be concise and concrete. For impact/"what breaks" questions, list the
  affected downstream datasets."""


@app.post("/api/ask")
def api_ask(req: AskRequest) -> dict:
    if not llm.is_configured():
        raise HTTPException(400, "LLM not configured — set GROQ_API_KEY.")
    if not req.question.strip():
        raise HTTPException(400, "Ask a question.")
    hits = store.search(req.question, k=req.k)
    blocks, cited = [], []
    for doc, score in hits:
        cited.append({"id": doc.id, "score": round(score, 3)})
        block = doc.text
        imp = graph.impact_of(doc.id)
        if imp["total_affected"]:
            block += (f"\nComputed impact: changing {doc.id} affects "
                      f"{imp['total_affected']} downstream datasets: "
                      f"{', '.join(imp['affected'])}.")
        blocks.append(block)
    context = "\n\n---\n\n".join(blocks) or "(no matching datasets)"
    answer = llm.chat([
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {req.question}"},
    ])
    return {"question": req.question, "answer": answer, "retrieved": cited}


@app.post("/api/extract-lineage")
def api_extract(req: ExtractRequest) -> dict:
    if not llm.is_configured():
        raise HTTPException(400, "LLM not configured — set GROQ_API_KEY.")
    if not req.sql.strip():
        raise HTTPException(400, "Provide some SQL.")

    known = {f"{ds.id}.{c.name}" for ds in graph.all_datasets() for c in ds.columns}
    extracted = extract_lineage(req.sql)
    report = validate_against_known(extracted, known)

    added = False
    if req.add_to_catalog and report["target"] != "unknown":
        src_tables = sorted({ref.rsplit(".", 1)[0]
                             for col in report["columns"] for ref in col["derived_from"]})
        existing = graph.get(report["target"])
        columns = [Column(name=c["name"], type="unknown", derived_from=c["derived_from"])
                   for c in report["columns"]]
        graph.add_dataset(Dataset(
            id=report["target"],
            name=existing.name if existing else report["target"],
            layer=existing.layer if existing else "mart",
            owner=existing.owner if existing else "unassigned",
            description=existing.description if existing else "Added via SQL lineage extraction.",
            tags=existing.tags if existing else ["ai-extracted"],
            sources=src_tables, columns=columns,
        ))
        graph.build_edges()
        reindex()
        added = True

    return {"target": report["target"], "columns": report["columns"],
            "rejected_refs": report["rejected_refs"], "added_to_catalog": added,
            "raw_model_output": extracted.raw_model_output}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")
