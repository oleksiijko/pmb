"""
Narrative arcs - the top tier of PMB v2.

Where this fits:
  Reflective memory (phase 1) understands individual events.
  Causation graph (phase 2) connects them locally.
  Arcs (phase 3) group sequences into stories with LLM-written summaries.

An arc is a coherent thread: "Postgres adoption journey", "Alice's onboarding",
"Q4 incident response". Events join an arc when the LLM (in sleep) decides
they extend an existing story or start a new one.

Why this matters:
  Multi-hop questions often ask about a STORY - "how did we end up using
  Redis?", "what's been happening with the auth refactor?". Pure retrieval
  returns scattered events. Arcs let recall return a SUMMARY plus the most
  relevant events, vastly improving recall quality for narrative questions.

Storage (schema v4):
  arcs(id, workspace_id, title, summary, status, first_event_ulid,
       last_event_ulid, n_events, created_at, last_updated)
  arc_events(arc_id, event_ulid)

Read path:
  Engine.recall detects broad / narrative questions and searches arc
  summaries via plain BM25 (cheap), pulls top-3 arcs, mixes their summaries
  AND their member events into the candidate pool.

Cost:
  Clustering pass = ~1 LLM call per new event (in sleep).
  Read path = 0 LLM calls; arc summaries searched as documents.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmb.core.events import Event


log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------

@dataclass
class Arc:
    id: int | None
    workspace_id: str
    title: str
    summary: str = ""
    status: str = "active"  # active | closed
    first_event_ulid: str | None = None
    last_event_ulid: str | None = None
    n_events: int = 0
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "summary": self.summary,
            "status": self.status, "n_events": self.n_events,
            "first_event_ulid": self.first_event_ulid,
            "last_event_ulid": self.last_event_ulid,
            "created_at": self.created_at, "last_updated": self.last_updated,
        }


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------

def create_arc(db_path, arc: Arc) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO arcs
              (workspace_id, title, summary, status, first_event_ulid,
               last_event_ulid, n_events, created_at, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (arc.workspace_id, arc.title, arc.summary, arc.status,
             arc.first_event_ulid, arc.last_event_ulid, arc.n_events,
             arc.created_at, arc.last_updated),
        )
        return int(cur.lastrowid)


def update_arc(db_path, arc_id: int, **fields) -> None:
    if not fields:
        return
    fields["last_updated"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in fields.keys())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE arcs SET {sets} WHERE id = ?",
            (*fields.values(), arc_id),
        )


def add_event_to_arc(db_path, arc_id: int, event_ulid: str) -> bool:
    """Insert or ignore. Returns True if newly added."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO arc_events (arc_id, event_ulid) VALUES (?, ?)",
            (arc_id, event_ulid),
        )
        if cur.rowcount == 0:
            return False
        # Also bump arc.n_events, last_event_ulid, last_updated
        conn.execute(
            "UPDATE arcs SET n_events = n_events + 1, "
            "  last_event_ulid = ?, last_updated = ? WHERE id = ?",
            (event_ulid, time.time(), arc_id),
        )
        return True


def list_arcs(
    db_path, workspace_id: str, status: str | None = "active", limit: int = 50,
) -> list[Arc]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if status:
            rows = conn.execute(
                "SELECT * FROM arcs WHERE workspace_id = ? AND status = ? "
                "ORDER BY last_updated DESC LIMIT ?",
                (workspace_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM arcs WHERE workspace_id = ? "
                "ORDER BY last_updated DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
    return [_row_to_arc(r) for r in rows]


def get_arc(db_path, arc_id: int) -> Arc | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM arcs WHERE id = ?", (arc_id,)).fetchone()
    return _row_to_arc(row) if row else None


def arcs_for_event(db_path, event_ulid: str) -> list[Arc]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT a.* FROM arcs a JOIN arc_events ae ON ae.arc_id = a.id "
            "WHERE ae.event_ulid = ?",
            (event_ulid,),
        ).fetchall()
    return [_row_to_arc(r) for r in rows]


def events_in_arc(db_path, arc_id: int) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT event_ulid FROM arc_events WHERE arc_id = ?",
            (arc_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _row_to_arc(r) -> Arc:
    return Arc(
        id=r["id"], workspace_id=r["workspace_id"], title=r["title"],
        summary=r["summary"] or "", status=r["status"],
        first_event_ulid=r["first_event_ulid"],
        last_event_ulid=r["last_event_ulid"],
        n_events=r["n_events"], created_at=r["created_at"],
        last_updated=r["last_updated"],
    )


# ----------------------------------------------------------------------
# LLM-based arc clustering
# ----------------------------------------------------------------------

ARC_CLUSTER_PROMPT = """\
You manage narrative threads in a memory system. A new event was logged.
Decide which existing arc it belongs to, OR propose a new arc.

Output VALID JSON with EXACTLY these keys:
  "action":      "join" | "create" | "ignore"
  "arc_id":      integer ID of existing arc to join, OR null
  "new_title":   short title (3-6 words) if creating a new arc, OR null
  "reasoning":   one sentence - why this fits / why new

Rules:
- "ignore" if the event is too generic / off-topic / doesn't belong in any narrative.
- "join" requires confidence the event continues an existing thread.
- "create" if it starts something new and substantive.
- New arc titles are noun phrases ("Postgres adoption journey", "Alice's onboarding").

New event:
\"\"\"
{event_content}
\"\"\"

Existing active arcs:
{arc_list}

JSON:"""


ARC_SUMMARY_PROMPT = """\
You are writing a summary for a narrative arc in a memory system.
Given the events below, write a short coherent summary (2-4 sentences,
under 300 chars). Capture the arc's overall meaning, not just a list.

Arc title: {title}

Events (chronological):
{events}

Summary (no preamble, no markdown):"""


class ArcManager:
    """Cluster events into narrative arcs via LLM."""

    def __init__(self, llm_client, max_arc_options: int = 8):
        self.llm = llm_client
        self.max_arc_options = max_arc_options

    def assign_event(
        self,
        event: Event,
        candidate_arcs: list[Arc],
        workspace_id: str,
    ) -> dict:
        """Decide where this event goes. Returns a dict with at least 'action'
        key: 'join'/'create'/'ignore'.
        Caller persists the result.
        """
        shortlist = candidate_arcs[:self.max_arc_options]
        arc_list_text = self._format_arc_list(shortlist)
        prompt = ARC_CLUSTER_PROMPT.format(
            event_content=(event.content or "")[:600],
            arc_list=arc_list_text,
        )
        try:
            text = self._call_llm(prompt)
        except Exception as e:
            log.warning("ArcManager LLM call failed: %s", e)
            return {"action": "ignore", "reason": str(e)[:80]}
        try:
            data = self._parse_json(text)
        except Exception as e:
            log.warning("ArcManager non-JSON output: %s | %s", e, text[:200])
            return {"action": "ignore", "reason": "non_json"}

        action = str(data.get("action", "ignore")).strip().lower()
        if action not in {"join", "create", "ignore"}:
            return {"action": "ignore"}
        out: dict = {"action": action, "reasoning": str(data.get("reasoning", ""))[:200]}
        if action == "join":
            try:
                arc_id = int(data.get("arc_id"))
            except (TypeError, ValueError):
                return {"action": "ignore", "reason": "bad_arc_id"}
            if not any(arc.id == arc_id for arc in shortlist):
                return {"action": "ignore", "reason": "arc_not_offered"}
            out["arc_id"] = arc_id
        elif action == "create":
            title = str(data.get("new_title", "")).strip()[:80]
            if not title:
                return {"action": "ignore", "reason": "no_title"}
            out["new_title"] = title
        return out

    def write_summary(self, arc: Arc, events: list[Event]) -> str:
        ev_text = "\n".join(
            f"  - [{int(e.timestamp)}] {(e.content or '')[:180]}" for e in events[:20]
        )
        prompt = ARC_SUMMARY_PROMPT.format(title=arc.title, events=ev_text)
        try:
            text = self._call_llm(prompt)
        except Exception as e:
            log.warning("ArcManager write_summary failed: %s", e)
            return arc.summary
        return text.strip()[:1000]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _format_arc_list(self, arcs: list[Arc]) -> str:
        if not arcs:
            return "  (no existing arcs)"
        return "\n".join(
            f"  [id={a.id}] {a.title}: {a.summary[:120]}" for a in arcs
        )

    def _call_llm(self, prompt: str) -> str:
        if hasattr(self.llm, "complete"):
            return self.llm.complete(prompt) or ""
        if callable(self.llm):
            return self.llm(prompt) or ""
        raise RuntimeError("ArcManager: llm must expose .complete() or be callable")

    def _parse_json(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found")
        return json.loads(text[start : end + 1])


# ----------------------------------------------------------------------
# Recall integration helpers
# ----------------------------------------------------------------------

# Pattern that suggests the user wants a narrative answer, not a specific fact
_NARRATIVE_QUERY_RE = re.compile(
    r"\b(history of|tell me about|what's been|how did .* end up|"
    r"summary of|overview|the story of|arc|journey|evolution|"
    r"what (?:has )?happened (?:with|to)|all about|what do (?:we|you) know about)\b",
    re.IGNORECASE,
)


def looks_narrative(query: str) -> bool:
    if not query:
        return False
    return bool(_NARRATIVE_QUERY_RE.search(query))
