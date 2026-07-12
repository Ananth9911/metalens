"""
sql_lineage.py — LLM-powered column-level lineage extraction from SQL.

This is the piece that answers "are we doing something automatic?": yes.
Instead of hand-declaring which column feeds which (the manual approach a
plain catalog forces), we hand raw SQL to an LLM and ask it to infer the
column-level lineage as structured JSON.

Why an LLM and not a pure SQL parser? A deterministic parser (sqlglot etc.)
is more precise for standard SQL, but brittle across dialects and helpless
with expressions like `COALESCE(a, b) AS c`. An LLM generalises across
dialects and messy real-world SQL. In an interview this is the honest
tradeoff to state: LLM = flexible + dialect-agnostic, but needs validation;
parser = exact but rigid. Here we use the LLM and then *validate* its output
against the tables we actually know about, rejecting hallucinated columns.
"""

import json
from dataclasses import dataclass

from .llm import chat_json


EXTRACTION_SYSTEM_PROMPT = """You are a data-lineage extraction engine.
Given one or more SQL statements that create or populate a target table,
identify COLUMN-LEVEL lineage: for each output column of the target table,
which source table.column values it is derived from.

Rules:
- Resolve table aliases to their real table names.
- A column may derive from zero, one, or many source columns
  (e.g. COALESCE(a,b) -> both a and b; COUNT(*) -> the grouping key columns).
- Use fully-qualified names "schema.table.column" when a schema is present,
  otherwise "table.column".
- Only include source columns that actually appear in the SQL.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{
  "target": "schema.table",
  "columns": [
    {"name": "col_a", "derived_from": ["src_schema.src_table.src_col"]},
    {"name": "col_b", "derived_from": []}
  ]
}"""


@dataclass
class ExtractedLineage:
    target: str
    columns: list[dict]        # [{"name","derived_from":[...]}, ...]
    raw_model_output: str      # kept for transparency / debugging


def extract_lineage(sql: str, model: str | None = None) -> ExtractedLineage:
    """Send SQL to the LLM and parse structured lineage back."""
    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": f"Extract lineage from this SQL:\n\n```sql\n{sql}\n```"},
    ]
    text = chat_json(messages, model=model)
    data = _safe_parse(text)
    return ExtractedLineage(
        target=data.get("target", "unknown"),
        columns=data.get("columns", []),
        raw_model_output=text,
    )


def _safe_parse(text: str) -> dict:
    """LLMs sometimes wrap JSON in ```json fences or add stray prose.
    Strip that and parse defensively."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # drop the first fence line and any trailing fence
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    cleaned = cleaned.strip().strip("`").strip()
    # find the outermost JSON object if there is leading/trailing noise
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"target": "unknown", "columns": []}


def validate_against_known(
    extracted: ExtractedLineage, known_columns: set[str]
) -> dict:
    """
    Guard against hallucination: drop any derived_from reference the LLM
    invented that doesn't correspond to a column we actually know about.
    Returns a report so the UI can show what was kept vs rejected.
    """
    kept, rejected = [], []
    clean_columns = []
    for col in extracted.columns:
        good_refs, bad_refs = [], []
        for ref in col.get("derived_from", []):
            if ref in known_columns:
                good_refs.append(ref)
            else:
                bad_refs.append(ref)
        clean_columns.append({"name": col["name"], "derived_from": good_refs})
        kept.extend(good_refs)
        rejected.extend(bad_refs)
    return {
        "target": extracted.target,
        "columns": clean_columns,
        "kept_refs": kept,
        "rejected_refs": rejected,
    }
