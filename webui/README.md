# ASTRA demo console (`webui/`)

A web front end for presenting ASTRA. It walks through one node's whole life:

1. the existing network;
2. the join request;
3. the Layer 1 agentic investigation and its Trust Factor;
4. the administrator's decision;
5. Layer 2 transaction monitoring;
6. revocation.

## Run

From the repository root, with the project's virtual environment:

```powershell
.venv\Scripts\python.exe webui\server.py
```

Then open http://127.0.0.1:8600.

Use **Reset demo** (top right) before you present. It deletes
`webui/demo.sqlite3` and starts a fresh network.

## What is real and what is simulated

The following come unchanged from the `astra` package:

- Ed25519 identities, signed transfers and replica validation;
- the LangGraph investigation and its read-only tool contract;
- the Trust Factor policy;
- the SQLite lifecycle and audit trail;
- the Layer 2 rolling-window checks.

During Layer 2, every member transaction is a real signed transfer. Each
replica validates it. A double-spend attempt reuses a nonce, and the replicas
refuse it.

Two inputs are synthetic:

- **Layer 2 observer counts.** Block share, competing tips and pool yield come
  from `astra.monitoring.synthetic_batch`.
- **The analyst, when no LLM is configured.** If `ASTRA_MODEL` and
  `ASTRA_BASE_URL` are not set, a deterministic stand-in (`analyst.py`) answers
  the model's calls using the same category rules as the small-model prompts.
  The header pill always shows which analyst is in use.

To use Qwen3 through Ollama instead, set the same `.env` values as for the
Streamlit console.

## Files

- `server.py`: FastAPI JSON API, the live ledger for Layer 2, and background
  investigations.
- `analyst.py`: the deterministic stand-in analyst.
- `static/`: the page (`index.html`, `app.css`, `app.js`). Plain HTML, CSS and
  JS, with no build step.
