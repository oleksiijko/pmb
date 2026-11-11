"""Tests for the auto-recall hook (intent classifier + dispatcher).

The classifier is regex-only, so this file primarily covers the intent
matrix in three languages (en/ru/uk) plus the dispatcher with a fake
engine that records what got called.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pmb.hooks.auto_recall import (
    AutoContextResult,
    Intent,
    detect_intents,
    format_context,
    is_trivial,
    run_auto_context,
)

# ─── Trivial-input detection ────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "",
    "  ",
    "hi",
    "hello!",
    "thanks",
    "thank you",
    "ok",
    "kk",
    "got it",
    "nice",
    "ок",        # length-trivial (< min_chars) regardless of language
    "ага",
    "🚀",
    "🤔🤔",
])
def test_trivial_messages(msg):
    # G3: multi-char RU/UK acks (привет/спасибо/…) are now WARM trivial_ack
    # anchors, not a cold pack list; cold is_trivial keeps the EN + length floor.
    assert is_trivial(msg) is True
    assert detect_intents(msg) == [Intent.SKIP]


@pytest.mark.parametrize("msg", [
    "fix the recall bug in PMB",
    "когда я последний раз делал ремонт",
    "what are my open goals",
    "какие правила у проекта PMB",
])
def test_non_trivial_messages(msg):
    assert is_trivial(msg) is False


# ─── PAST_QUERY ─────────────────────────────────────────────────────────


# G3: the COLD lexical tier is now EN-only — RU/UK intents are WARM-anchor
# classified (see test_semantic_intent::test_anchor_intent_real_multilingual,
# which covers ru/uk). These cold cases keep the English regex matrix honest.
@pytest.mark.parametrize("msg", [
    "what did I just decide about auth",  # also matches RECENT
    "when did we switch to pnpm",
    "why did we choose Vite",
    "where did I put the env file",
    "who said the build was broken",
    "what's my postgres port",
    "have I ever used Tailscale",
    "do we have a rule about logging",
])
def test_past_query(msg):
    intents = detect_intents(msg)
    assert Intent.PAST_QUERY in intents, f"missed PAST_QUERY in {msg!r}"


# ─── RECENT_QUERY ───────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "what did I just do",
    "what are we currently working on",
    "what's happening",
    "what's going on",
])
def test_recent_query(msg):
    assert Intent.RECENT_QUERY in detect_intents(msg)


# ─── GOALS_QUERY ────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "my open goals",
    "open goals",
    "current goals",
    "what am I working on",
    "todo list",
    "what's left to do",
    "what should I do next",
    "remaining tasks",
])
def test_goals_query(msg):
    assert Intent.GOALS_QUERY in detect_intents(msg)


# ─── LESSONS_QUERY ──────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "what lessons do we have for testing",
    "do we use pnpm here",
    "do we have a rule about logging",
    "conventions for this repo",
])
def test_lessons_query(msg):
    assert Intent.LESSONS_QUERY in detect_intents(msg)


# ─── PROJECT_PREP / PROJECT_OVERVIEW ────────────────────────────────────


def test_project_prep_with_work_verb():
    intents = detect_intents(
        "fix the recall bug in PMB", known_projects={"PMB"},
    )
    assert Intent.PROJECT_PREP in intents


def test_project_overview_ru_project_name():
    # G3: project NAME detection is language-agnostic (substring), so a RU
    # sentence still finds the project — but the RU work VERB ("почини") is a
    # warm-anchor signal now, so cold detection yields OVERVIEW, not PREP.
    intents = detect_intents(
        "почини docker-compose в LeanBoard", known_projects={"LeanBoard"},
    )
    assert Intent.PROJECT_OVERVIEW in intents


def test_project_overview_uk_project_name():
    intents = detect_intents(
        "виправ assertion у LoadGuard", known_projects={"LoadGuard"},
    )
    assert Intent.PROJECT_OVERVIEW in intents


def test_project_overview_without_work_verb():
    intents = detect_intents("tell me about PMB", known_projects={"PMB"})
    assert Intent.PROJECT_OVERVIEW in intents
    assert Intent.PROJECT_PREP not in intents


def test_project_word_boundary_no_partial_match():
    # "PMBish" must NOT trigger a PMB project hit.
    intents = detect_intents("fix the PMBish parser", known_projects={"PMB"})
    assert Intent.PROJECT_PREP not in intents
    assert Intent.PROJECT_OVERVIEW not in intents


def test_no_project_known_no_project_intent():
    intents = detect_intents("fix the recall bug in PMB", known_projects=set())
    assert Intent.PROJECT_PREP not in intents
    assert Intent.PROJECT_OVERVIEW not in intents


# ─── GENERIC_FACTUAL fallback ───────────────────────────────────────────


def test_generic_factual_question_mark():
    # No project, no explicit pattern — but ends in '?'.
    intents = detect_intents("could the indexer skip symlinks?")
    assert Intent.GENERIC_FACTUAL in intents


def test_no_question_no_generic_factual():
    intents = detect_intents("could the indexer skip symlinks")
    assert Intent.GENERIC_FACTUAL not in intents


def test_multiple_intents_combined():
    # Both PAST_QUERY and project mention (EN cold path).
    intents = detect_intents(
        "when did I last fix PMB recall?",
        known_projects={"PMB"},
    )
    assert Intent.PAST_QUERY in intents
    # Verb of doing? "fix" → matches WORK_VERB.
    assert Intent.PROJECT_PREP in intents


# ─── Dispatcher with a fake engine ──────────────────────────────────────


@dataclass
class FakeRecallPack:
    results_data: list[dict]
    confidence_val: float = 0.7
    elapsed_ms: float = 5.0

    def to_dict(self):
        return {
            "results": self.results_data,
            "confidence": self.confidence_val,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class FakeEngine:
    """Records what the dispatcher called. Minimal API surface."""
    calls: list[str] = field(default_factory=list)
    known_entities: list[dict] = field(default_factory=list)
    detected_project: dict | None = None
    project_overview_result: dict | None = None
    recall_results: list[dict] = field(default_factory=list)
    recent_result: list[dict] = field(default_factory=list)
    activity_result: list[dict] = field(default_factory=list)
    goals_result: list[dict] = field(default_factory=list)
    lessons_result: list[dict] = field(default_factory=list)
    decisions_result: list[dict] = field(default_factory=list)
    arcs_result: list[dict] = field(default_factory=list)
    confidence: float = 0.7
    surface_log: list[tuple] = field(default_factory=list)

    def graph_top_entities(self, kind=None, limit=200):
        self.calls.append(f"graph_top_entities(kind={kind})")
        return self.known_entities

    def detect_project_in_text(self, text):
        self.calls.append(f"detect_project_in_text({text[:30]!r})")
        return self.detected_project

    def project_overview(self, name):
        self.calls.append(f"project_overview({name!r})")
        return self.project_overview_result

    def active_arcs_for_project(self, name, limit=2):
        self.calls.append(f"active_arcs_for_project({name!r})")
        return self.arcs_result

    def recall(self, query, top_k=5):
        self.calls.append(f"recall({query[:30]!r}, top_k={top_k})")
        return FakeRecallPack(
            results_data=self.recall_results,
            confidence_val=self.confidence,
        )

    def what_just_happened(self, n=5):
        self.calls.append(f"what_just_happened(n={n})")
        return self.recent_result

    def recent_activity(self, minutes, limit):
        self.calls.append(f"recent_activity({minutes},{limit})")
        return self.activity_result

    def list_goals(self, status=None, limit=50):
        self.calls.append(f"list_goals({status!r})")
        return self.goals_result

    def find_lessons(self, query="", limit=5, project=None):
        self.calls.append(
            f"find_lessons({query[:30]!r}, limit={limit}, project={project!r})"
        )
        return self.lessons_result

    def find_decisions(self, query="", limit=3):
        self.calls.append(f"find_decisions({query[:30]!r}, limit={limit})")
        return self.decisions_result

    def _log_lesson_surfaces(self, lessons, query="", source=""):
        self.surface_log.append((len(lessons), source))


def test_dispatch_cold_skip_for_recall():
    """Past-query intent + cold engine → recall() must NOT be called.

    The hook spawns fresh each turn; recall would load sentence-transformers
    from disk and add ~15s. Other intents still work because they're SQL-only.
    """

    @dataclass
    class ColdEngine(FakeEngine):
        def is_warm(self):
            return False

    eng = ColdEngine(
        recall_results=[{"content": "should not show", "score": 0.9}],
        lessons_result=[{"content": "L1", "surface_id": 1}],
    )
    res = run_auto_context(eng, "when did I last test docker")
    assert Intent.PAST_QUERY in res.intents
    assert "RECALL_COLD_SKIP" in res.intents
    assert res.recall_hits == []  # cold → skipped
    # But lessons still fired (pure SQL, no embed cost).
    assert res.lessons == [{"content": "L1", "surface_id": 1}]
    # And NO call to recall happened.
    assert not any(c.startswith("recall(") for c in eng.calls)


def test_dispatch_warm_allows_recall():
    """Past-query intent + warm engine → recall() runs normally."""

    @dataclass
    class WarmEngine(FakeEngine):
        def is_warm(self):
            return True

    eng = WarmEngine(
        recall_results=[{"content": "found it", "score": 0.85}],
        confidence=0.85,
    )
    res = run_auto_context(eng, "when did I last test docker")
    assert "RECALL_COLD_SKIP" not in res.intents
    assert res.recall_hits and res.recall_hits[0]["content"] == "found it"


def test_dispatch_skip_on_trivial():
    eng = FakeEngine()
    res = run_auto_context(eng, "ok")
    assert res.skipped is True
    assert res.intents == [Intent.SKIP]
    # Skip path is cheap: only is_trivial check, no engine queries.
    assert eng.calls == []


@pytest.mark.parametrize("msg", [
    # Markers that were already skipped: these must keep passing.
    "<task-notification>\n<task-id>x</task-id>\n<status>completed</status>\n</task-notification>",
    "<system-reminder>\nThe task tools haven't been used recently.\n</system-reminder>",
    # Claude Code subagent hand-backs reach the hook as a user prompt (issue #84).
    '<agent-message from="a1b2c3">\n[Subagent hand-back] The text below is the final report of '
    "a subagent.\n  Done: updated settings.json hooks and verified the JSON parses.\n"
    "</agent-message>",
    '<AGENT-MESSAGE from="a1b2c3">\nThis was fixed again, it was wrong before.\n</AGENT-MESSAGE>',
    "[Subagent hand-back] The text below is the final report of a subagent.\nFixed it again.",
])
def test_dispatch_skips_machine_generated_blocks(msg):
    """Task notifications, system reminders and subagent hand-backs are not user requests."""
    eng = FakeEngine()
    res = run_auto_context(eng, msg)
    assert res.skipped is True
    assert res.skip_reason == "non-message (system/tool block)"
    assert res.intents == [Intent.SKIP]
    # No auto-context, no correction capture: the engine must not be queried at all.
    assert eng.calls == []


def test_dispatch_past_query_calls_recall():
    eng = FakeEngine(
        recall_results=[
            {"content": "Postgres port is 5433", "score": 0.85, "date": "2026-05-12"},
        ],
        confidence=0.85,
    )
    res = run_auto_context(eng, "what's my postgres port")
    assert Intent.PAST_QUERY in res.intents
    assert res.recall_hits and res.recall_hits[0]["content"].startswith("Postgres")
    assert any(c.startswith("recall(") for c in eng.calls)


def test_dispatch_generic_factual_filters_low_confidence():
    # Low-score result should be filtered out from generic-factual recall.
    eng = FakeEngine(
        recall_results=[{"content": "irrelevant", "score": 0.10}],
        confidence=0.10,
    )
    res = run_auto_context(eng, "what is the cap on int?")
    assert Intent.GENERIC_FACTUAL in res.intents
    assert res.recall_hits == []  # filtered


def test_dispatch_project_prep_full_pipeline():
    eng = FakeEngine(
        known_entities=[{"name": "PMB"}],
        detected_project={"name": "PMB"},
        project_overview_result={
            "entity": {"name": "PMB", "n_mentions": 42},
            "key_facts": [{"content": "uses paraphrase-multilingual-MiniLM-L12-v2"}],
            "lessons": [{"content": "unrelated broad project lesson", "surface_id": 9}],
            "decisions": [{"content": "dropped LanceDB"}],
            "open_goals": [{"title": "ship v1"}],
        },
        lessons_result=[
            {"content": "run the recall regression tests", "surface_id": 1},
        ],
        arcs_result=[{"title": "auth refactor", "n_events": 5}],
    )
    res = run_auto_context(eng, "fix recall bug in PMB")
    assert Intent.PROJECT_PREP in res.intents
    assert res.project is not None
    assert res.arcs and res.arcs[0]["title"] == "auth refactor"
    # Format check.
    text = format_context(res)
    assert "PMB" in text
    assert "run the recall regression tests" in text
    assert "unrelated broad project lesson" not in text
    assert "surface_id=1" in text


def test_dispatch_recent_query_calls_what_just_happened():
    eng = FakeEngine(
        recent_result=[{"content": "fixed JWT bug"}],
    )
    res = run_auto_context(eng, "what did we just do")
    assert Intent.RECENT_QUERY in res.intents
    assert res.recent and res.recent[0]["content"] == "fixed JWT bug"


def test_dispatch_goals_query():
    eng = FakeEngine(goals_result=[{"title": "ship v1", "status": "in_progress"}])
    res = run_auto_context(eng, "what are my open goals")
    assert Intent.GOALS_QUERY in res.intents
    assert res.open_goals


def test_dispatch_lessons_always_surface():
    # Even for a PAST_QUERY intent (no LESSONS_QUERY), lessons run with
    # limit=3 as a side-dish.
    eng = FakeEngine(
        recall_results=[{"content": "X", "score": 0.5}],
        lessons_result=[{"content": "pnpm only", "surface_id": 1}],
    )
    res = run_auto_context(eng, "when did I choose pnpm")
    assert res.lessons
    # And surface-log fired once.
    assert eng.surface_log and eng.surface_log[0][1] == "hook.auto_recall"


def test_dispatch_lessons_skip_log_when_disabled():
    eng = FakeEngine(
        lessons_result=[{"content": "pnpm only", "surface_id": 1}],
    )
    res = run_auto_context(eng, "what did I learn about deploy", log_surfaces=False)
    assert res.lessons
    assert eng.surface_log == []


def test_dispatch_surfaces_decisions():
    eng = FakeEngine(
        recall_results=[{"content": "X", "score": 0.5}],
        decisions_result=[
            {"ulid": "d1", "content": "Chose Playwright over Cypress for e2e"},
        ],
    )
    res = run_auto_context(eng, "when did I choose the e2e framework")
    assert res.decisions and res.decisions[0]["content"].startswith("Chose Playwright")
    assert any(c.startswith("find_decisions(") for c in eng.calls)
    text = format_context(res)
    assert "Past decisions about this" in text
    assert "Playwright" in text


def test_dispatch_decisions_disabled():
    eng = FakeEngine(
        lessons_result=[{"content": "L", "surface_id": 1}],
        decisions_result=[{"ulid": "d1", "content": "some decision"}],
    )
    res = run_auto_context(eng, "what did I learn about deploy", surface_decisions=False)
    assert res.decisions == []
    assert not any(c.startswith("find_decisions(") for c in eng.calls)


def test_dispatch_decisions_deduped_against_project():
    # A decision already in project_overview shouldn't be repeated.
    eng = FakeEngine(
        known_entities=[{"name": "PMB"}],
        detected_project={"name": "PMB"},
        project_overview_result={
            "entity": {"name": "PMB", "n_mentions": 5},
            "decisions": [{"ulid": "d1", "content": "already in overview"}],
        },
        decisions_result=[
            {"ulid": "d1", "content": "already in overview"},
            {"ulid": "d2", "content": "fresh decision"},
        ],
    )
    res = run_auto_context(eng, "fix bug in PMB")
    ulids = {d["ulid"] for d in res.decisions}
    assert "d1" not in ulids   # deduped
    assert "d2" in ulids


def test_format_context_empty_returns_empty_string():
    res = AutoContextResult(message="", intents=[Intent.SKIP], skipped=True)
    assert format_context(res) == ""


def test_format_context_truncates_to_max_chars():
    res = AutoContextResult(
        message="x",
        intents=[Intent.PAST_QUERY],
        recall_hits=[{"content": "A" * 1000, "score": 0.9, "date": "2026-01-01"}] * 5,
        recall_query="x",
    )
    out = format_context(res, max_chars=400)
    assert len(out) <= 400
    assert "context truncated" in out
