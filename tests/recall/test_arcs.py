"""Tests for narrative arcs (PMB v2 Phase 3).

The claim: events get clustered into named threads (arcs) with LLM-written
summaries. Narrative-style queries find them. Multi-event arcs surface
their members together so the LLM has full story context."""
from __future__ import annotations

import json

import numpy as np

from pmb.core.engine import Engine
from pmb.reasoning.arcs import (
    Arc,
    add_event_to_arc,
    create_arc,
    events_in_arc,
    get_arc,
    looks_narrative,
)

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------





# ----------------------------------------------------------------------
# Stub LLM for arc clustering
# ----------------------------------------------------------------------

class _ArcStub:
    """Returns scripted assign / summary responses."""

    def __init__(self, decisions: list[dict], summaries: list[str] = None):
        self.decisions = list(decisions)
        self.summaries = list(summaries or [])
        self.calls_assign = 0
        self.calls_summary = 0

    def complete(self, prompt: str, **kw) -> str:
        # Detect prompt kind by content
        if "Existing active arcs" in prompt:
            out = self.decisions[self.calls_assign]
            self.calls_assign += 1
            return json.dumps(out)
        else:
            # Summary prompt
            if not self.summaries:
                return "An arc summary."
            out = self.summaries[self.calls_summary]
            self.calls_summary += 1
            return out


# ----------------------------------------------------------------------
# Looks-narrative classifier
# ----------------------------------------------------------------------

def test_looks_narrative_matches_story_queries():
    assert looks_narrative("tell me about the postgres migration")
    assert looks_narrative("what's been happening with Alice")
    assert looks_narrative("history of the auth refactor")


def test_looks_narrative_ignores_simple_queries():
    assert not looks_narrative("postgres port")
    assert not looks_narrative("how do I run tests")


# ----------------------------------------------------------------------
# Arc CRUD
# ----------------------------------------------------------------------

def test_create_and_list_arc(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    arc_id = create_arc(eng.workspace.db_path, Arc(
        id=None,
        workspace_id=eng.workspace.id,
        title="Postgres adoption",
        summary="Project chose Postgres over MySQL",
    ))
    arcs = eng.list_arcs()
    assert len(arcs) == 1
    assert arcs[0]["title"] == "Postgres adoption"
    assert arcs[0]["id"] == arc_id


def test_add_event_to_arc(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    arc_id = create_arc(eng.workspace.db_path, Arc(
        id=None, workspace_id=eng.workspace.id, title="X",
    ))
    e1 = eng.record_fact("First event in X")
    e2 = eng.record_fact("Second event in X")
    assert add_event_to_arc(eng.workspace.db_path, arc_id, e1)
    assert add_event_to_arc(eng.workspace.db_path, arc_id, e2)
    arc = get_arc(eng.workspace.db_path, arc_id)
    assert arc.n_events == 2
    ulids = events_in_arc(eng.workspace.db_path, arc_id)
    assert set(ulids) == {e1, e2}


# ----------------------------------------------------------------------
# LLM-driven clustering via engine.cluster_events_into_arcs
# ----------------------------------------------------------------------

def test_cluster_creates_new_arc(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    eng.record_fact("Started thinking about switching to Postgres")
    stub = _ArcStub(
        decisions=[{
            "action": "create", "arc_id": None,
            "new_title": "Postgres adoption journey",
            "reasoning": "fresh thread",
        }],
        summaries=["Project began considering Postgres."],
    )
    result = eng.cluster_events_into_arcs(limit=10, llm=stub)
    assert result["n_created"] == 1
    assert result["n_summaries_updated"] == 1
    arcs = eng.list_arcs()
    assert len(arcs) == 1
    assert "Postgres" in arcs[0]["title"]
    assert "Postgres" in arcs[0]["summary"]


def test_cluster_joins_existing_arc(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    # Manually create an arc
    arc_id = create_arc(eng.workspace.db_path, Arc(
        id=None, workspace_id=eng.workspace.id,
        title="Postgres adoption journey",
        summary="...",
    ))
    eng.record_fact("Picked Postgres 17 for the api service")
    stub = _ArcStub(
        decisions=[{
            "action": "join", "arc_id": arc_id,
            "reasoning": "fits the postgres thread",
        }],
        summaries=["Postgres 17 chosen for api."],
    )
    result = eng.cluster_events_into_arcs(limit=10, llm=stub)
    assert result["n_joined"] == 1
    arc = get_arc(eng.workspace.db_path, arc_id)
    assert arc.n_events == 1


def test_cluster_ignores_off_topic(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    eng.record_fact("Random unrelated note")
    stub = _ArcStub(decisions=[{
        "action": "ignore", "reasoning": "too generic",
    }])
    result = eng.cluster_events_into_arcs(limit=10, llm=stub)
    assert result["n_ignored"] == 1
    assert result["n_created"] == 0


def test_cluster_offers_similar_arc_even_outside_recent_pool(
    tmp_pmb_home, tmp_workspace_dir, monkeypatch,
):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    db_path, workspace_id = eng.workspace.db_path, eng.workspace.id
    expected = create_arc(db_path, Arc(
        id=None, workspace_id=workspace_id, title="Postgres migration",
    ))
    for i in range(51):
        create_arc(db_path, Arc(
            id=None, workspace_id=workspace_id, title=f"Unrelated frontend change {i}",
        ))
    eng.record_fact("Postgres will be the production database")

    def embed_batch(texts):
        return np.array([(1, 0) if "Postgres" in text else (0, 1) for text in texts])

    def complete(prompt):
        assert f"[id={expected}] Postgres migration" in prompt
        assert prompt.count("[id=") == 8
        return json.dumps({"action": "join", "arc_id": expected})

    monkeypatch.setattr(eng.search, "embed_batch", embed_batch)
    result = eng.cluster_events_into_arcs(llm=complete, refresh_summaries=False)
    assert result["n_joined"] == 1
    assert result["n_created"] == 0
    assert get_arc(db_path, expected).n_events == 1


# ----------------------------------------------------------------------
# Recall integration
# ----------------------------------------------------------------------

def test_arc_expansion_surfaces_member_events(tmp_pmb_home, tmp_workspace_dir):
    """Narrative-style query finds the arc summary AND pulls in member
    events for full story context."""
    eng = Engine(
        cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home,
        config_overrides={
            "recall.cache_size": 0,
            "recall.arc_expansion": True,
            "recall.arc_boost": 0.5,  # strong for test signal
            "recall.spreading_activation": False,
        },
    )
    # Three events that form a story
    e1 = eng.record_fact("Started evaluating database options for the project")
    e2 = eng.record_fact("Compared MySQL JSONB support vs Postgres JSONB")
    e3 = eng.record_fact("Decided on Postgres 17 with WAL replication")
    # Bind them to an arc
    arc_id = create_arc(eng.workspace.db_path, Arc(
        id=None, workspace_id=eng.workspace.id,
        title="Postgres adoption journey",
        summary="Project evaluated databases and chose Postgres 17 with WAL.",
    ))
    for u in (e1, e2, e3):
        add_event_to_arc(eng.workspace.db_path, arc_id, u)

    # Decoy events outside the arc, no Postgres mention
    eng.record_fact("Lunch with the design team")
    eng.record_fact("Reviewed Q3 financials")

    # Narrative query — should surface ALL three arc events even if BM25
    # would only find some
    pack = eng.recall("tell me about the postgres adoption", top_k=10)
    ulids = {r.ulid for r in pack.results}
    # All three arc members should appear
    assert e1 in ulids
    assert e2 in ulids
    assert e3 in ulids
