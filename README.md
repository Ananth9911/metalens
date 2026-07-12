# MetaLens — data lineage, impact analysis & AI catalog

**Live demo:** https://metalens-ananth-container.azurewebsites.net/

A data catalog that combines a **graph-based lineage engine** with **LLM-powered
SQL extraction and RAG question-answering**, over one shared catalog. Containerized
with Docker and deployed to Azure App Service with a full CI/CD pipeline.

![graph](docs/graph.png)

---

## What it does

| Tab | Capability | Powered by |
|---|---|---|
| **Graph** | Interactive lineage map — click any dataset for upstream/downstream, impact blast radius, pipeline build order | Graph algorithms (no LLM required) |
| **Ask** | Plain-English questions answered from the catalog, with dataset citations | RAG (embeddings → retrieval → LLM) |
| **Extract from SQL** | Paste SQL → column-level lineage inferred and inserted into the graph | LLM + hallucination validation |

The two halves are genuinely integrated: **lineage extracted from SQL is written
into the same graph**, so a newly-extracted table immediately appears in the graph
view, participates in impact analysis, and becomes retrievable in AI answers.

---

## Engineering highlights

**Graph engine** (`backend/lineage.py`) — dependency-free, standard library only:
- DFS transitive closure for upstream/downstream lineage — O(V+E)
- BFS with parent-pointer reconstruction for shortest lineage path
- **Kahn's algorithm** for topological build order
- DFS grey/black colouring for **cycle detection** (broken-pipeline detection)
- Column-level provenance tracing back to raw sources

**RAG pipeline** (`backend/rag.py`):
- Each dataset is serialized to a retrievable document; questions are embedded and
  matched by **cosine similarity** over an in-memory NumPy vector store
- Impact questions are **grounded in the real computed downstream set** from the
  graph — the model answers from facts, not guesses
- Embeddings load **on a background thread** so the container answers its health
  probe immediately; falls back to a dependency-free hashing embedder if the neural
  model is unavailable

**Structured extraction with a validation guard** (`backend/sql_lineage.py`):
- The LLM returns JSON lineage; every inferred source column is then **validated
  against the known catalog and hallucinated references are rejected and surfaced**
- LLM output is treated as untrusted and verified — not blindly accepted

---

## Architecture

```
  Local code ──► Docker image ──► Azure Container Registry ──► Azure App Service
                                                                  (Linux container)
                                                                         │
  GitHub ──► GitHub Actions (OIDC) ──► build & push ──────────────────────┘
                                                                         │
                                                              FastAPI / Uvicorn
                                                                         │
                                                                    Groq API
```

**Runtime stack:** FastAPI · Python 3.12 · Docker · Uvicorn · Groq
**Cloud:** Azure App Service (Linux container) · Azure Container Registry · Managed Identity
**CI/CD:** GitHub Actions with OIDC federated credentials — no stored passwords

### Why Docker
ZIP/Oryx deployment failed repeatedly with `ModuleNotFoundError` and startup/path
inconsistencies caused by environment differences between local and Azure's build
container. Containerizing removed the variable entirely: the image that runs locally
is byte-for-byte the image that runs in production.

### Secrets handling
- `GROQ_API_KEY` lives in **Azure App Settings** (server-side) — never in the repo,
  never in frontend JavaScript.
- The repo ships a blank `.env.example`; `.env` is git-ignored.
- The container pulls from ACR using **Managed Identity + AcrPull**, not a registry
  password. GitHub authenticates to Azure via **OIDC**, not a publish profile.

---

## Repository layout

```
├─ Dockerfile              # the canonical build
├─ app.py                  # ASGI entry point
├─ requirements.txt
├─ backend/
│  ├─ app.py               # FastAPI: graph + AI endpoints, serves the UI
│  ├─ lineage.py           # graph engine (DFS, BFS, Kahn, cycle detection)
│  ├─ rag.py               # embeddings + cosine-similarity vector store
│  ├─ sql_lineage.py       # LLM SQL→lineage + hallucination validator
│  └─ llm.py               # Groq client (key from environment)
├─ data/catalog.yaml       # sample warehouse (raw → staging → mart → report)
├─ frontend/               # Graph / Ask / Extract UI (no build step)
└─ .github/workflows/      # CI/CD: build → ACR → App Service
```

---

## Run locally

### Docker (matches production exactly)
```bash
docker build -t metalens .
docker run -p 8000:8000 -e GROQ_API_KEY=<your_key> metalens
# open http://localhost:8000
```

### Python
```bash
pip install -r requirements.txt
cp .env.example .env          # add your free Groq key: https://console.groq.com/keys
uvicorn app:app --reload --port 8000
```

The **Graph tab works without any API key.** The Ask and Extract tabs require
`GROQ_API_KEY`.

---

## Deploying

Every push to `main` triggers GitHub Actions, which builds the Docker image, pushes
it to Azure Container Registry, and the Web App pulls the new image automatically.

```bash
git add .
git commit -m "message"
git push origin main          # → build → ACR → live
```

**Azure resources**

| Resource | Name |
|---|---|
| Resource Group | `metalens-rg` |
| Container Registry | `metalensananth9911` |
| App Service Plan | `metalens-docker-plan` (Linux, B1) |
| Web App | `metalens-ananth-container` |

**Useful commands**
```bash
az webapp log tail --resource-group metalens-rg --name metalens-ananth-container
az acr repository list --name metalensananth9911 -o table
```

---

## API

| Method | Path | Key? | Description |
|---|---|---|---|
| GET | `/api/health` | — | status, LLM configured, embedding backend, graph stats |
| GET | `/api/datasets` | — | list / filter datasets |
| GET | `/api/lineage/{id}` | — | lineage subgraph |
| GET | `/api/impact/{id}` | — | downstream impact grouped by layer |
| GET | `/api/build-order` | — | topological build order + cycle check |
| POST | `/api/ask` | ✓ | RAG answer with citations |
| POST | `/api/extract-lineage` | ✓ | SQL → column lineage (+ optional insert into graph) |

Interactive docs at [`/docs`](https://metalens-ananth-container.azurewebsites.net/docs).

---

## Scope & honesty

A focused demo, not a production data platform. Deliberately out of scope: a real
vector database (the store is in-memory NumPy — the retrieval interface is identical,
so swapping in FAISS/pgvector is localized), catalog persistence (it's a YAML file),
authentication, and a deterministic SQL parser (an LLM is used, with validation —
the tradeoff is documented in `sql_lineage.py`).

The service **degrades gracefully**: graph features need no LLM, embeddings fall back
to a hashing model if the neural one can't load, and AI endpoints return a clear error
rather than crashing when no key is configured.

### Next steps
- Application Insights for monitoring and request tracing
- Swap the in-memory store for a vector DB; persist the catalog in Postgres
- Add a deterministic SQL parser (sqlglot) and reconcile it against the LLM output
- Health checks, autoscaling, and Infrastructure-as-Code (Bicep/Terraform)

## License
MIT — see [LICENSE](LICENSE).