"""Auto-recall - zero-cooperation memory injection.

PMB's biggest UX problem: the agent has to remember to call `recall` /
`prepare` / `project_overview`. It often doesn't. The Adherence dashboard
shows `prepare_rate` near 0% on most workspaces - instructions in
CLAUDE.md are good intentions, not enforcement.

This module fixes the dependency. It runs from the UserPromptSubmit hook
(`pmb hooks install`) and decides - without asking the model - which PMB
calls to pre-execute and inject as context. Pure regex intent
classification + parallel-safe dispatch over the engine. No LLM, no
network, no API keys. Sub-100ms p95 on a warm workspace.

Intents (multilingual, RU/UK/EN):

  PROJECT_PREP        a known project name + a work-verb (fix/add/refactor +
                      RU/UK equivalents). → full project_overview + arcs + goals.
  PROJECT_OVERVIEW    a known project name standalone. → project_overview.
  PAST_QUERY          "what did I / why did we" + RU/UK equivalents.
                      → recall(message).
  RECENT_QUERY        "what did I just / what are we doing now" + RU/UK.
                      → what_just_happened(5).
  GOALS_QUERY         "open goals / what's left" + RU/UK equivalents.
                      → list_goals(in_progress).
  LESSONS_QUERY       "what rules / lessons / conventions" + RU/UK.
                      → find_lessons(message).
  GENERIC_FACTUAL     question mark + non-trivial content, no other match.
                      → recall(message, top_k=3), surface only if score > 0.3.
  SKIP                trivial (greeting / ack / very short / pure-code).

Lessons are *always* surfaced (cheap, high value) in addition to whatever
intent fires. The CLI formats the result into a plain-text block that the
agent's host appends to the prompt.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from pmb import lang as _lang

# ─── Intents ─────────────────────────────────────────────────────────────


class Intent:
    PROJECT_PREP = "PROJECT_PREP"
    PROJECT_OVERVIEW = "PROJECT_OVERVIEW"
    PAST_QUERY = "PAST_QUERY"
    RECENT_QUERY = "RECENT_QUERY"
    GOALS_QUERY = "GOALS_QUERY"
    LESSONS_QUERY = "LESSONS_QUERY"
    GENERIC_FACTUAL = "GENERIC_FACTUAL"
    WORK_REQUEST = "WORK_REQUEST"
    SKIP = "SKIP"


# ─── Patterns ────────────────────────────────────────────────────────────
#
# Three-language coverage (en/ru/uk). Anchored to whole-word boundaries
# where reasonable. Tested in tests/test_auto_recall.py.

def _ialt(en_frags: list[str], *cats: str) -> str:
    """Join English inline regex fragments with the RU/UK fragments an active
    lang pack contributes for `cats`, into one alternation body (no outer
    group). Keeps this module Cyrillic-free (L1): the EN fragments stay inline,
    the Cyrillic equivalents live in packs/{ru,uk}.yaml. Order is irrelevant -
    these feed iterate-all `search()` matchers."""
    frags = list(en_frags)
    for c in cats:
        frags.extend(str(x) for x in _lang.merged_list(c) if str(x).strip())
    return "|".join(frags)


# "what did I / when did I / why did we / where did / who did" + the RU/UK
# equivalents (pack: intent_past_query). Includes mild typos.
_PAST_QUERY = re.compile(
    r"(?:" + _ialt([
        r"\bwhat\s+did\s+(?:i|we)\s", r"\bwhen\s+did\s+(?:i|we)",
        r"\bwhy\s+did\s+we", r"\bwhere\s+did\s+(?:i|we)",
        r"\bwho\s+(?:is|was|said|did)", r"\bwhat'?s\s+(?:my|the\s+)",
        r"\bhow\s+come", r"\bdo\s+we\s+have", r"\bhave\s+i\s+(?:ever|already)",
    ], "intent_past_query") + r")",
    re.IGNORECASE,
)

# "what did I just / what are we doing right now" + RU/UK (intent_recent_query).
_RECENT_QUERY = re.compile(
    r"(?:" + _ialt([
        r"\bwhat\s+(?:did|are)\s+(?:i|we)\s+(?:just|currently|right\s+now)",
        r"\bwhat'?s\s+(?:going\s+on|happening)",
    ], "intent_recent_query") + r")",
    re.IGNORECASE,
)

# Open goals / "what's left to do" + RU/UK equivalents (intent_goals_query).
_GOALS_QUERY = re.compile(
    r"(?:" + _ialt([
        r"\bmy\s+(?:open\s+)?goals?", r"\bopen\s+goals?", r"\bin\s+flight",
        r"\bwhat\s+am\s+i\s+working\s+on", r"\bcurrent\s+goals?",
        r"\bto-?do\b", r"\bwhat'?s\s+(?:left|next)\b", r"\bwhat\s+is\s+left\b",
        r"\bremaining\s+(?:tasks?|work|items?)\b",
        r"\bwhat\s+(?:do\s+i\s+still\s+need|should\s+i\s+do\s+next)",
    ], "intent_goals_query") + r")",
    re.IGNORECASE,
)

# Project rules / lessons / conventions + RU/UK (intent_lessons_query).
_LESSONS_QUERY = re.compile(
    r"(?:" + _ialt([
        r"\bconvention", r"\blesson", r"\brule\s+(?:about|for)",
        r"\bdo\s+we\s+use\b", r"\bdo\s+we\s+have\s+a\s+rule",
    ], "intent_lessons_query") + r")",
    re.IGNORECASE,
)

# Work verbs that, combined with a project name, mean "I'm about to work on
# it". The RU/UK verb stems live in the packs (work_verb_markers); EN inline.
# Intentionally generous.
_WORK_VERB = re.compile(
    r"(?:" + _ialt([
        r"\b(?:fix|fixing|add|adding|refactor|refactoring|implement|"
        r"implementing|build|building|write|writing|debug|debugging|"
        r"deploy|deploying|test|testing|continue|review|reviewing|"
        r"port|porting|migrate|migrating|update|updating|rewrite|"
        r"rewriting|wire|wiring|patch|patching|land|ship|push|"
        # R4: common imperative work verbs the old list missed
        r"tighten|optimiz\w*|optimise|clean(?:\s*up)?|simplif\w*|harden|"
        r"improve|remove|delete|rename|extract|split|merge|handle|"
        r"enable|disable|configure|set\s*up|setup|install|upgrade|"
        r"rework|tweak|hook\s*up|integrate|finish|finalize|finalise)\b",
        r"\bworking\s+on\b", r"\bwork\s+on\b",
    ], "work_verb_markers") + r")",
    re.IGNORECASE,
)

# Trivial input: greetings, acks, single emoji, very short. RU/UK acks live in
# the packs (trivial_acks); EN inline.
_TRIVIAL = re.compile(
    r"^[\s\W_]*(?:" + _ialt([
        r"hi", r"hello", r"hey", r"yo", r"ok", r"okay", r"kk", r"got\s+it",
        r"sure", r"thanks", r"thank\s+you", r"ty", r"tysm", r"cheers", r"nice",
        r"cool", r"np", r"good\s+morning", r"good\s+night", r"gn", r"gm",
    ], "trivial_acks") + r")[\s\W_]*$",
    re.IGNORECASE,
)

# A '?' (incl. fullwidth) anywhere is a strong question signal.
_HAS_QUESTION = re.compile(r"[?？]")


# ─── API ─────────────────────────────────────────────────────────────────


def is_trivial(msg: str, min_chars: int = 5) -> bool:
    """True for greetings, acks, single emoji, or very-short input."""
    s = (msg or "").strip()
    if len(s) < min_chars:
        return True
    if _TRIVIAL.match(s):
        return True
    return False


def detect_intents(
    msg: str,
    known_projects: set[str] | None = None,
    min_chars: int = 5,
) -> list[str]:
    """Classify a user message into a list of intents.

    `known_projects` is a set of project-entity names this workspace
    actually knows about. Without that hint we can't tell a project name
    apart from a random capitalized noun.

    Returns a list (order = priority). [Intent.SKIP] alone means
    nothing fires and the hook should output nothing.
    """
    s = (msg or "").strip()
    if is_trivial(s, min_chars=min_chars):
        return [Intent.SKIP]

    out: list[str] = []

    # Project detection - purely substring (case-insensitive). Avoid
    # spurious matches by requiring word-boundary. `known_projects` come
    # from engine.detect_project_in_text or the entity graph.
    project_hit = False
    if known_projects:
        s_l = s.lower()
        for name in known_projects:
            if not name:
                continue
            name_l = name.lower()
            # Use a regex word-boundary anchor so "PMBish" doesn't match "PMB"
            # but allow Cyrillic project names too.
            if re.search(rf"(?:^|\W){re.escape(name_l)}(?:$|\W)", s_l):
                project_hit = True
                break

    if project_hit:
        if _WORK_VERB.search(s):
            out.append(Intent.PROJECT_PREP)
        else:
            out.append(Intent.PROJECT_OVERVIEW)

    # Past / recent / goals / lessons - explicit question patterns.
    if _RECENT_QUERY.search(s):
        out.append(Intent.RECENT_QUERY)
    if _PAST_QUERY.search(s):
        out.append(Intent.PAST_QUERY)
    if _GOALS_QUERY.search(s):
        out.append(Intent.GOALS_QUERY)
    if _LESSONS_QUERY.search(s):
        out.append(Intent.LESSONS_QUERY)

    # A question fires a specific fact-recall when nothing else would surface
    # the answer: either NO intent matched, OR only a project intent did - a
    # project-NAMED question ("what is <project>'s default timezone?") must
    # surface the specific fact, not just the project overview. The recall is
    # low-cost and self-gated (only surfaces when it actually hits), so adding
    # it on top of a project intent is safe.
    if _HAS_QUESTION.search(s) and (
        not out
        or Intent.PROJECT_OVERVIEW in out
        or Intent.PROJECT_PREP in out
    ):
        out.append(Intent.GENERIC_FACTUAL)

    # R4: a WORK REQUEST - an imperative / work verb, no project, no question.
    # "tighten the retry logic" / "refactor the auth module" used to return
    # [SKIP] (no "?"), so the agent did real work with ZERO surfaced lessons or
    # decisions. Fire a non-SKIP intent so the always-on lessons + decisions
    # side-dish runs (cheap SQL, no semantic recall, no project_overview).
    if not out and _WORK_VERB.search(s):
        out.append(Intent.WORK_REQUEST)

    return out or [Intent.SKIP]


# ─── Dispatch ────────────────────────────────────────────────────────────


@dataclass
class AutoContextResult:
    """What the dispatcher returns. CLI / hook formats this for the model."""

    message: str
    intents: list[str]
    project: dict | None = None        # project_overview output
    arcs: list[dict] = field(default_factory=list)
    recall_hits: list[dict] = field(default_factory=list)
    recall_query: str | None = None
    recall_confidence: float = 0.0
    recent: list[dict] = field(default_factory=list)
    open_goals: list[dict] = field(default_factory=list)
    lessons: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    # Correction capture + repeat guard (stage-1 of the lesson funnel).
    correction: dict | None = None       # {severity, surface_id, reused, markers}
    loud_lessons: list[dict] = field(default_factory=list)  # rules to show LOUD
    latency_ms: int = 0
    skipped: bool = False
    skip_reason: str | None = None

    def is_empty(self) -> bool:
        """True if nothing useful matched - hook should print nothing."""
        return not any([
            self.project,
            self.arcs,
            self.recall_hits,
            self.recent,
            self.open_goals,
            self.lessons,
            self.decisions,
            self.correction,
            self.loud_lessons,
        ])


def _known_projects(engine) -> set[str]:
    """Known project-entity names, memoized by write-generation (S5).

    The uncached form runs `graph_top_entities(200)` (~27 ms) and fires on
    nearly every hook message; the set only changes when the corpus changes,
    so we key it on the recall cache's generation counter (bumped on every
    write) and recompute only after a write."""
    try:
        gen = getattr(engine.recall_cache, "_generation", 0)
    except Exception:
        gen = 0
    cached = getattr(engine, "_known_projects_cache", None)
    if cached is not None and cached[0] == gen:
        return cached[1]
    out = _known_projects_uncached(engine)
    try:
        engine._known_projects_cache = (gen, out)
    except Exception:
        pass
    return out


def _known_projects_uncached(engine) -> set[str]:
    """Pull all known project-entity names this workspace has.

    Combines two sources:
      1. The workspace's own name (always treat it as a known project -
         on fresh workspaces the graph hasn't extracted entities yet,
         and we still want "fix bug in <workspace>" to trigger PREP).
      2. graph_top_entities - the canonical entity-set used by
         engine.detect_project_in_text. Only includes the kinds that
         could plausibly be projects (skip 1-char / pure-digit noise).
    """
    out: set[str] = set()

    # 1. Workspace name as a default project.
    try:
        ws_name = (engine.workspace.name or "").strip()
        if ws_name and len(ws_name) >= 2 and not ws_name.isdigit():
            out.add(ws_name)
    except Exception:
        pass

    # 2. Graph entities - but ONLY plausible PROJECT entities (R5). The old
    # `kind=None, len>=2` swept in every concept the extractor emitted -
    # 'tests' / 'fails' / 'cloud' (concept) and mis-classified tool names as
    # 'person' - so "fix the tests" faked a PROJECT_PREP and a junk
    # project_overview ate the context budget ahead of the lessons. A graph
    # entity now has to EARN "known project" status: a project-ish kind, a real
    # recurrence (n_mentions >= floor), and a name that isn't a bare stopword.
    # (The workspace-name default added above bypasses this - the user's own
    # project always fires.)
    try:
        entities = engine.graph_top_entities(kind=None, limit=200)
    except Exception:
        return out
    # A SMALL generic function-word set - NOT text_match.STOPWORDS, which also
    # contains dev-noise like 'pmb'/'code'/'test'/'file' that are perfectly
    # valid PROJECT names. We only want to reject bare grammar words here.
    _NAME_STOP = {
        "the", "a", "an", "is", "are", "was", "were", "this", "that", "it",
        "and", "or", "but", "of", "in", "on", "to", "for", "with", "as",
        "what", "who", "where", "when", "why", "how", "which",
    }
    _PROJECT_KINDS = {
        "project", "repo", "repository", "product", "codebase",
        "app", "application", "service", "system",
    }
    try:
        min_m = int(engine.config.get("auto_recall.project_min_mentions") or 3)
    except Exception:
        min_m = 3
    for e in entities or []:
        name = e.get("name") or e.get("normalized_name")
        if not (name and isinstance(name, str) and len(name) >= 2):
            continue
        if name.isdigit() or name.strip().lower() in _NAME_STOP:
            continue
        kind = (e.get("kind") or "").strip().lower()
        if kind and kind not in _PROJECT_KINDS:
            continue
        n_m = int(e.get("n_mentions") or e.get("mentions") or e.get("count") or 0)
        if n_m and n_m < min_m:
            continue
        out.add(name)
    return out


def _resolve_project_name(
    engine,
    msg: str,
    known_projects: set[str],
) -> str | None:
    """Pick the project name to dispatch on.

    Strategy:
      1. Ask engine.detect_project_in_text with a relaxed threshold -
         even 1 mention is enough for the auto-recall hook (we'd
         rather over-fire than miss).
      2. If that returns nothing, fall back to a longest-substring
         match against `known_projects` (which already includes the
         workspace name + graph entities).
    """
    try:
        det = engine.detect_project_in_text(msg, min_mentions=1)
        if det and det.get("name"):
            return det["name"]
    except Exception:
        pass
    s_l = (msg or "").lower()
    for name in sorted(known_projects, key=len, reverse=True):
        if not name:
            continue
        if re.search(rf"(?:^|\W){re.escape(name.lower())}(?:$|\W)", s_l):
            return name
    return None


def _specificity_ok(message: str, hit: dict, strong_cosine: float) -> bool:
    """Specificity gate for GENERIC_FACTUAL recall surfacing.

    A hit that already cleared the absolute-evidence floor is surfaced only if
    it is genuinely SPECIFIC to the message: it shares >= 1 distinctive token
    with the message, OR its absolute embedding similarity is strong on its own
    (>= strong_cosine). This separates real matches from 'same-domain but
    unhelpful' hits - the topically-adjacent results (moderate ~0.06 cosine,
    zero lexical overlap) that the evidence floor alone cannot tell apart from
    real ones (both land ~0.05-0.10). Measured on the real corpus: genuine hits
    have a distinctive lexical overlap OR raw_cosine >= ~0.08; the same-domain
    noise has neither. strong_cosine <= 0 disables the gate (always passes).
    """
    if strong_cosine <= 0:
        return True
    sig = hit.get("signals") or {}
    if float(sig.get("raw_cosine") or 0.0) >= strong_cosine:
        return True
    from pmb.core.text_match import distinctive_tokens
    return bool(distinctive_tokens(message)
                & distinctive_tokens(hit.get("content") or ""))


def _looks_conversational(
    message: str, hits: list, confidence: float,
    gap_max: float, conf_max: float,
) -> bool:
    """Query-worthiness gate: True when a GENERIC_FACTUAL message is really
    CONVERSATIONAL / meta ('is it better now?', 'did you test it?') rather than
    a knowledge query, so the Context (recall) channel should surface nothing.

    Signature of a conversational turn (measured on a labelled set): the recall
    is DIFFUSE - small top1->top2 score gap (no clear winner) AND low confidence
    - AND the top hit shares no distinctive token with the message. Any one of:
    a real lexical anchor, a clear winner (gap >= gap_max), or high confidence
    means we treat it as a genuine query and keep it. This catches the
    same-domain noise the absolute-cosine specificity gate cannot (a 0.09-cosine
    off-topic hit). Disabled when either threshold is <= 0.
    """
    if gap_max <= 0 or conf_max <= 0 or not hits:
        return False
    if confidence >= conf_max:
        return False  # confident -> a real query
    s0 = float(hits[0].get("score") or 0.0)
    s1 = float(hits[1].get("score") or 0.0) if len(hits) > 1 else 0.0
    if (s0 - s1) >= gap_max:
        return False  # a clear winner -> a real query
    from pmb.core.text_match import distinctive_tokens
    if distinctive_tokens(message) & distinctive_tokens(hits[0].get("content") or ""):
        return False  # has a lexical anchor -> a real query
    return True


_PASTED_RE = re.compile(
    r"<pasted_content(?:\s[^<>]*)?>.*?(?:</pasted_content\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def run_auto_context(
    engine,
    message: str,
    *,
    min_chars: int = 5,
    recall_top_k: int = 5,
    recall_min_score: float = 0.30,
    recall_evidence_min: float = 0.0,   # R3: absolute raw-cosine gate (0 = off)
    specificity_strong_cosine: float = 0.0,  # specificity gate (0 = off)
    conversational_gap_max: float = 0.0,     # cheap conversational gate (0 = off)
    conversational_conf_max: float = 0.0,    # cheap conversational gate (0 = off)
    query_worthiness_tau: float = 0.0,       # SAE query-worthiness gate (warm)
    lessons_limit: int = 5,
    decisions_limit: int = 3,
    surface_decisions: bool = True,
    recent_minutes: float = 1440.0,
    recent_limit: int = 8,
    goals_limit: int = 5,
    log_surfaces: bool = True,
    correction_capture: bool = True,
    correction_record_draft: bool = True,
    correction_importance: float = 0.85,
    repeat_guard: bool = True,
) -> AutoContextResult:
    """Classify the message and dispatch the matching PMB queries.

    Pure orchestration - no I/O outside the engine itself. All branches
    are wrapped in best-effort try/except so a single failure doesn't
    blank the whole context.
    """
    t0 = time.perf_counter()
    msg = (message or "").strip()
    res = AutoContextResult(message=msg, intents=[])

    if not msg:
        res.intents = [Intent.SKIP]
        res.skipped = True
        res.skip_reason = "empty message"
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        return res

    # Non-message noise: task-notification / system-reminder / subagent
    # hand-back / raw tool-output blocks get routed through the hook the same
    # as user text, but they are NOT requests. Surfacing memory on them is pure
    # noise - on the real workspace this was ~half of all lesson surfaces.
    # Skip before classifying.
    _head = msg.lstrip()[:120].lower()
    if _head.startswith((
        "<task-notification>", "<system-reminder>", "[system notification",
        "<tool-use-id>", "<command-name>", "<local-command",
        "<agent-message", "[subagent hand-back]",
        "here is a note offered by a side agent",
    )):
        res.intents = [Intent.SKIP]
        res.skipped = True
        res.skip_reason = "non-message (system/tool block)"
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        return res

    # Trivial skip BEFORE touching the engine - saves a DB roundtrip on
    # greetings/acks/very-short input. `is_trivial` is regex-only.
    if is_trivial(msg, min_chars=min_chars):
        res.intents = [Intent.SKIP]
        res.skipped = True
        res.skip_reason = "trivial"
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        return res

    # Correction capture runs on EVERY non-trivial message, BEFORE intent
    # classification. An angry correction (profanity + "did not fill in again") is
    # usually NOT a question, so it would classify as SKIP and inject nothing -
    # which is exactly the moment the rule needs to be captured (the RR
    # failure: the locate-me lesson was written only after the 7th complaint).
    correction_sig = None
    # Only the user's own words can be a correction: text the user pasted
    # (Claude Code wraps it in <pasted_content>) was written by someone else.
    own = _PASTED_RE.sub(" ", msg).strip() if correction_capture else ""
    if own:
        try:
            from pmb.hooks.correction_capture import detect_correction
            correction_sig = detect_correction(own)
        except Exception:
            correction_sig = None
    if correction_sig:
        info = {"severity": correction_sig.severity, "surface_id": None,
                "reused": False, "markers": correction_sig.markers}
        if correction_record_draft:
            # Scope the draft to the project the complaint is about, so it
            # surfaces only for that project and the agent sees what it relates
            # to. Use the SAME resolver the overview path uses - it keeps dev-ish
            # names like 'pmb' that the engine's stopword-filtered detector drops.
            try:
                corr_project = _resolve_project_name(
                    engine, msg, _known_projects(engine))
            except Exception:
                corr_project = None
            try:
                # Quote the user's words, not a paste: the draft keeps only
                # the first 280 characters.
                cap = engine.capture_correction(
                    own, severity=correction_sig.severity,
                    markers=correction_sig.markers,
                    importance=correction_importance,
                    project=corr_project,
                )
                info["surface_id"] = cap.get("surface_id")
                info["reused"] = cap.get("reused", False)
                info["project"] = cap.get("project")
            except Exception:
                pass
        res.correction = info

    # Step 1: classify (non-trivial: load known projects from the graph).
    try:
        known = _known_projects(engine)
    except Exception:
        known = set()
    intents = detect_intents(msg, known_projects=known, min_chars=min_chars)
    res.intents = intents

    # D3 shadow-T1 (sampled): when the COLD lexical tier fired a recall intent,
    # occasionally check the WARM anchor and log whether they agreed, so the
    # distiller can prune an auto.yaml category that misleads the cold path.
    # Warm-only, gated (lang.anchor_log), ~5% sample - negligible cost.
    try:
        import random as _rnd
        if (intents != [Intent.SKIP] and _rnd.random() < 0.05
                and hasattr(engine, "is_warm") and engine.is_warm()
                and engine.config.get("lang.anchor_log")):
            from pmb.maintenance.distill import (
                _INTENT_TO_CATEGORY,
                record_shadow_t1,
            )
            # NB: do NOT reuse the name `t0` here - it is the perf-counter start
            # set above and read at the end for latency_ms. Shadowing it with the
            # intent (which can be None) crashed latency math on the 5% sample.
            cat_intent = next((i for i in intents if i in _INTENT_TO_CATEGORY), None)
            if cat_intent:
                from pmb.hooks.semantic_intent import classify_anchor_intent
                record_shadow_t1(
                    engine, cat_intent,
                    classify_anchor_intent(engine, msg) == cat_intent)
    except Exception:
        pass

    if intents == [Intent.SKIP]:
        # C5: lexical detection found nothing. If semantic intents are enabled
        # AND the engine is warm (daemon-served - never load the model on the
        # cold per-process hook path), try an embedding-based classification so
        # a query in a language the lexical patterns don't cover still fires.
        sem = None
        try:
            warm = hasattr(engine, "is_warm") and engine.is_warm()
            # B1: the calibrated anchor tier (`lang.anchors`, on by default) is
            # the multilingual fallback; the legacy centroid tier still honours
            # the explicit `hooks.semantic_intents` opt-in. Either way we only
            # touch the model when WARM (daemon-served) - never on the cold hook.
            want_sem = warm and (
                engine.config.get("lang.anchors")
                or engine.config.get("hooks.semantic_intents"))
            if want_sem:
                from pmb.hooks.semantic_intent import classify_semantic_intent
                sem = classify_semantic_intent(
                    engine, msg,
                    threshold=float(engine.config.get(
                        "hooks.semantic_intent_threshold") or 0.45))
        except Exception:
            sem = None
        if sem:
            intents = [sem]
            res.intents = intents + ["SEMANTIC_INTENT"]
        elif res.correction is not None:
            # A correction with no other intent must STILL inject (the draft +
            # the loud guard). Carry on with no intent branches; the always-on
            # lessons block + repeat guard below still run.
            intents = []
            res.intents = ["CORRECTION"]
        else:
            # Non-trivial but no intent matched. Still give the REPEAT GUARD a
            # chance: a re-raised complaint ("why X again") often strongly
            # overlaps an existing rule even when it's not a question and trips
            # no intent. Surface that rule LOUD - but ONLY if it strongly
            # matches; otherwise inject nothing (don't add generic-lesson noise
            # to plain statements). One indexed find_lessons on an otherwise-
            # skipped message.
            if repeat_guard:
                try:
                    from pmb.hooks.correction_capture import strong_lesson_matches
                    cand = engine.find_lessons(query=msg, limit=8)
                    loud = strong_lesson_matches(msg, cand or [], limit=3)
                    if loud:
                        res.loud_lessons = loud
                        res.intents = ["REPEAT_GUARD"]
                        if log_surfaces:
                            try:
                                engine._log_lesson_surfaces(
                                    [L for L in loud if L.get("ulid")],
                                    query=msg, source="hook.repeat_guard")
                            except Exception:
                                pass
                        res.latency_ms = int((time.perf_counter() - t0) * 1000)
                        return res
                except Exception:
                    pass
            # Safety net - detect_intents shouldn't return SKIP for non-trivial
            # input, but if it does, treat the same as trivial.
            res.skipped = True
            res.skip_reason = "no-intent-matched"
            res.latency_ms = int((time.perf_counter() - t0) * 1000)
            return res

    # Step 2: dispatch per intent.
    # PROJECT_PREP / OVERVIEW share the project_overview call; PREP also
    # pulls active arcs + open goals as extras.
    if Intent.PROJECT_PREP in intents or Intent.PROJECT_OVERVIEW in intents:
        try:
            project_name = _resolve_project_name(engine, msg, known)
            if project_name:
                ov = engine.project_overview(project_name)
                if ov and not ov.get("empty"):
                    res.project = ov
                    if Intent.PROJECT_PREP in intents:
                        try:
                            arcs = engine.active_arcs_for_project(
                                project_name, limit=2,
                            )
                            if arcs:
                                res.arcs = arcs
                        except Exception:
                            pass
        except Exception:
            pass

    # PAST_QUERY / GENERIC_FACTUAL: ask the recall layer.
    # Cold-start guard: the hook spawns a fresh Python process each turn,
    # so loading sentence-transformers from disk for every recall would
    # add 10-20s per user message. Skip the recall when the engine isn't
    # warm - the model loads in the BACKGROUND for the next turn, and
    # the other intents (project/lessons/recent/goals) still fire from
    # pure-SQL paths. To keep recall always-on, run `pmb warmup` once
    # after install to seed the on-disk cache.
    do_recall = (
        Intent.PAST_QUERY in intents
        or Intent.GENERIC_FACTUAL in intents
    )
    if do_recall:
        warm = True
        try:
            # is_warm() lives on the engine.search submodule (via embed mixin)
            # but historically callers ran `engine.is_warm()` too. Try both.
            if hasattr(engine, "is_warm"):
                warm = bool(engine.is_warm())
            elif hasattr(engine, "search") and hasattr(engine.search, "is_warm"):
                warm = bool(engine.search.is_warm())
        except Exception:
            warm = True  # be permissive if probe fails
        if not warm:
            # Note the cold-skip in intents so the agent / debug output
            # makes it obvious why recall didn't fire.
            res.intents = res.intents + ["RECALL_COLD_SKIP"]
        else:
            try:
                pack = engine.recall(query=msg, top_k=recall_top_k)
                pd = pack.to_dict() if pack else {}
                hits = pd.get("results") or []
                conf = float(pd.get("confidence") or 0.0)
                # GENERIC_FACTUAL is best-effort - only surface if the top
                # hit is reasonably confident. PAST_QUERY is explicit so
                # we always surface what we got.
                # R3: the `score` gate runs on a MIN-MAX-normalized scale (top
                # hit ≈ 1.0 even for an irrelevant corpus), so it nearly always
                # passes - the main false-positive channel. When
                # recall_evidence_min > 0, ALSO require the top hit's ABSOLUTE
                # vector similarity (signals.raw_cosine) to clear the bar, so a
                # query the workspace knows nothing about surfaces nothing.
                # Default 0.0 = off (eval-gated; flip on after V1 tunes it).
                def _abs_ok(h: dict) -> bool:
                    if recall_evidence_min <= 0:
                        return True
                    sig = h.get("signals") or {}
                    return float(sig.get("raw_cosine") or 0.0) >= recall_evidence_min
                # SAE query-worthiness (warm only): is this a conversational/meta
                # turn rather than a knowledge query? Catches what the cheap
                # gap/conf/lexical gate misses. Only for GENERIC_FACTUAL - an
                # explicit PAST_QUERY always surfaces.
                qw_conv = False
                if Intent.PAST_QUERY not in intents and query_worthiness_tau:
                    try:
                        _qw = engine.query_worthiness()
                        if _qw is not None:
                            qw_conv = _qw.is_conversational(msg, query_worthiness_tau)
                    except Exception:
                        qw_conv = False
                if hits and (
                    Intent.PAST_QUERY in intents
                    or (hits[0].get("score", 0.0) >= recall_min_score
                        and _abs_ok(hits[0])
                        and _specificity_ok(
                            msg, hits[0], specificity_strong_cosine)
                        and not _looks_conversational(
                            msg, hits, conf,
                            conversational_gap_max, conversational_conf_max)
                        and not qw_conv)
                ):
                    res.recall_hits = hits
                    res.recall_query = msg
                    res.recall_confidence = conf
            except Exception:
                pass

    if Intent.RECENT_QUERY in intents:
        try:
            recent = engine.what_just_happened(n=recent_limit)
            if recent:
                res.recent = recent
            else:
                act = engine.recent_activity(
                    minutes=recent_minutes, limit=recent_limit,
                )
                if act:
                    res.recent = act
        except Exception:
            pass

    if Intent.GOALS_QUERY in intents:
        try:
            goals = engine.list_goals(status="in_progress", limit=goals_limit)
            if goals:
                res.open_goals = goals
        except Exception:
            pass

    # Lessons: always run (cheap, high value). If LESSONS_QUERY was the
    # explicit intent we pull a bigger window; otherwise just 3.
    try:
        limit = lessons_limit if Intent.LESSONS_QUERY in intents else 3
        project_scope = None
        if res.project:
            project_scope = (res.project.get("entity") or {}).get("name")
        try:
            lessons = engine.find_lessons(
                query=msg, limit=limit, project=project_scope,
            )
        except TypeError:
            # Compatibility with light test doubles / older engines.
            lessons = engine.find_lessons(query=msg, limit=limit)
        if lessons:
            res.lessons = lessons
            if res.project is not None:
                # project_overview() is exhaustive when called directly;
                # automatic injection is intentionally message-specific.
                res.project["lessons"] = lessons
            if log_surfaces:
                try:
                    engine._log_lesson_surfaces(
                        lessons, query=msg, source="hook.auto_recall",
                    )
                except Exception:
                    pass
        elif res.project is not None:
            res.project["lessons"] = []
    except Exception:
        if res.project is not None:
            res.project["lessons"] = []

    # Decisions (the "why we did X" rationale): always run when enabled.
    # Surfacing past decisions next to lessons means "before you do this,
    # here's what we already decided about it" - the agent doesn't have to
    # think to ask. Only attached when find_decisions exists (newer engine).
    if surface_decisions and hasattr(engine, "find_decisions"):
        try:
            decs = engine.find_decisions(query=msg, limit=decisions_limit)
            if decs:
                # Don't repeat decisions the project_overview already surfaced.
                seen = set()
                pc = res.project or {}
                for d in (pc.get("decisions") or []):
                    u = d.get("ulid")
                    if u:
                        seen.add(u)
                res.decisions = [d for d in decs if d.get("ulid") not in seen]
        except Exception:
            pass

    # Repeat guard: if the message strongly overlaps a lesson that ALREADY
    # exists, promote it to a LOUD banner at the top of the block instead of
    # leaving it as one line in a wall the agent habituates to. Reuses the
    # lessons already fetched (no extra query). Surface-log the loud ones too
    # so follow-through is tracked.
    if repeat_guard:
        try:
            from pmb.hooks.correction_capture import strong_lesson_matches
            pool = list(res.lessons or [])
            pool += (res.project or {}).get("lessons") or []
            loud = strong_lesson_matches(msg, pool, limit=3)
            if loud:
                res.loud_lessons = loud
                if log_surfaces:
                    try:
                        engine._log_lesson_surfaces(
                            [L for L in loud if L.get("ulid")],
                            query=msg, source="hook.repeat_guard",
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    res.latency_ms = int((time.perf_counter() - t0) * 1000)
    return res


# ─── Formatting ──────────────────────────────────────────────────────────


def _trim(s: Any, n: int = 200) -> str:
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    s = s.strip().replace("\n", " ")
    return s[:n]


def format_context(
    res: AutoContextResult,
    *,
    max_chars: int = 4000,
    include_trace: bool = True,
) -> str:
    """Render an AutoContextResult into a plain-text block for the agent.

    Empty input → empty output. Hosts truncate long context, so we cap to
    `max_chars` and trim with an explicit marker.
    """
    if res.skipped or res.is_empty():
        return ""

    buf: list[str] = []
    header = "== PMB auto-context =="
    if include_trace:
        header += (
            f"  [intents={','.join(res.intents)} "
            f"latency={res.latency_ms}ms]"
        )
    buf.append(header)
    buf.append(f"(matched on message: {_trim(res.message, 100)!r})")

    # ── LOUD top banner: correction capture + repeat guard ──────────────────
    # These go FIRST and shouty on purpose - they are the antidote to the
    # agent habituating to the wall of auto-context and repeating a mistake.
    loud_ulids: set[str] = set()
    if res.correction:
        sid = res.correction.get("surface_id")
        sev = res.correction.get("severity", "weak")
        buf.append("")
        buf.append("⚠️ CORRECTION DETECTED — the user is pushing back on something just done.")
        if sid:
            verb = "Re-using" if res.correction.get("reused") else "Saved"
            buf.append(f"   {verb} a DRAFT lesson [surface_id={sid}] ({sev} signal). BEFORE continuing:")
        else:
            buf.append("   BEFORE continuing:")
        buf.append("   1. STOP repeating the thing they're objecting to — fix THAT first.")
        buf.append("   2. Refine the draft into a concrete rule via record_batch "
                   "(one line: what to ALWAYS/NEVER do).")
        if sid:
            buf.append(f"   3. Then mark_lesson_followed(surface_id={sid}, "
                       "followed=True, note=\"<what you changed>\").")

    if res.loud_lessons:
        buf.append("")
        buf.append("⚠️ YOU'VE HIT THIS BEFORE — a recorded rule strongly matches this message:")
        for L in res.loud_lessons[:3]:
            u = L.get("ulid")
            if u:
                loud_ulids.add(u)
            sid = L.get("surface_id")
            tag = f" [surface_id={sid}]" if sid else ""
            buf.append(f"  ‼ {_trim(L.get('content',''), 220)}{tag}")
        buf.append("   Apply it NOW — do not rediscover it by trial and error.")

    if res.project and not res.project.get("empty"):
        pc = res.project
        ent = pc.get("entity") or {}
        buf.append(
            f"\nProject: {ent.get('name','?')} "
            f"({ent.get('n_mentions','?')} mentions)"
        )
        kf = pc.get("key_facts") or []
        if kf:
            buf.append("Key facts:")
            for f in kf[:5]:
                buf.append(f"  - {_trim(f.get('content',''), 160)}")
        proj_lessons = [L for L in (pc.get("lessons") or [])
                        if L.get("ulid") not in loud_ulids]
        if proj_lessons:
            buf.append("Project lessons (RULES - FOLLOW):")
            for L in proj_lessons[:5]:
                sid = L.get("surface_id")
                tag = f" [surface_id={sid}]" if sid else ""
                buf.append(f"  ! {_trim(L.get('content',''), 200)}{tag}")
        dec = pc.get("decisions") or []
        if dec:
            buf.append("Past decisions:")
            for d in dec[:3]:
                buf.append(f"  > {_trim(d.get('content',''), 160)}")
        og = pc.get("open_goals") or []
        if og:
            buf.append("Open goals on this project:")
            for g in og[:3]:
                buf.append(
                    "  * "
                    + _trim(g.get("content") or g.get("title", ""), 120)
                )

    if res.recall_hits:
        # The "information" channel: background context, NOT rules. Labelled
        # distinctly from lessons (the "rules" channel) so the agent weights
        # them differently - rules override behaviour, context just informs.
        buf.append(
            f"\nPossibly relevant context (background, not rules) for "
            f"{_trim(res.recall_query, 60)!r} "
            f"(confidence={res.recall_confidence:.2f}):"
        )
        for h in res.recall_hits[:5]:
            date = h.get("date") or ""
            stamp = f" [{date}]" if date else ""
            buf.append(
                f"  - {_trim(h.get('content',''), 180)}"
                f" (score={float(h.get('score') or 0.0):.2f}){stamp}"
            )

    if res.recent:
        buf.append("\nRecent activity:")
        for a in res.recent[:5]:
            buf.append(f"  - {_trim(a.get('content',''), 120)}")

    if res.open_goals and not (res.project and res.project.get("open_goals")):
        buf.append("\nOpen goals:")
        for g in res.open_goals[:5]:
            buf.append(
                "  * "
                + _trim(g.get("content") or g.get("title", ""), 120)
            )

    # Surface lessons separately ONLY if no project context already had them.
    proj_lessons = (res.project or {}).get("lessons") or []
    if res.lessons and not proj_lessons:
        shown = [L for L in res.lessons if L.get("ulid") not in loud_ulids]
        if shown:
            buf.append("\nLessons matching this message:")
            for L in shown[:5]:
                sid = L.get("surface_id")
                tag = f" [surface_id={sid}]" if sid else ""
                buf.append(f"  ! {_trim(L.get('content',''), 200)}{tag}")

    # Past decisions (rationale) - so the agent doesn't re-decide a settled
    # call. Surfaced only if project_overview didn't already include them.
    proj_decisions = (res.project or {}).get("decisions") or []
    if res.decisions and not proj_decisions:
        buf.append("\nPast decisions about this (don't re-litigate without reason):")
        for d in res.decisions[:5]:
            buf.append(f"  > {_trim(d.get('content',''), 200)}")

    if res.arcs:
        buf.append("\nActive narrative arcs:")
        for a in res.arcs[:2]:
            buf.append(
                f"  ~ {_trim(a.get('title',''), 120)}"
                f" ({a.get('n_events','?')} events)"
            )

    # Footer: explicit instruction to use lessons + close the feedback loop.
    if res.lessons or proj_lessons or res.loud_lessons:
        buf.append("")
        buf.append(
            "If a lesson with [surface_id=N] applies, FOLLOW it. After acting,"
        )
        buf.append(
            "call mark_lesson_followed(surface_id=N, followed=True, note=\"...\")."
        )

    # Coordinate with the deliberate MCP channel: this block was injected by the
    # auto-recall HOOK, not requested. Tell the agent so it doesn't spend a turn
    # re-calling prepare()/recall() for what's already here.
    buf.append("")
    buf.append("(auto-recall ran for this message - only call recall() / "
               "find_lessons() for specifics not shown above.)")

    text = "\n".join(buf)
    if len(text) > max_chars:
        text = text[: max_chars - 30].rstrip() + "\n... [context truncated]"
    return text


def compute_prepare_context_text(engine, message: str,
                                 max_chars: int = 4000) -> str | None:
    """Run the auto-recall pipeline for `message` and return the formatted
    context text, or None when there is nothing to inject.

    Single source of truth shared by the `pmb prepare-context` CLI command
    (cold, per-process) and the daemon's /internal/hook/prepare-context
    endpoint (warm), so hook output is identical whichever path served it.
    Honors the same config knobs as the CLI command. Returns None when
    auto-recall is disabled (the caller may fall back to the legacy bundle)."""
    msg = (message or "").strip()
    if not msg:
        return None
    if not bool(engine.config.get("auto_recall.enabled")):
        return None
    res = run_auto_context(
        engine, msg,
        min_chars=int(engine.config.get("auto_recall.min_message_chars") or 5),
        recall_top_k=int(engine.config.get("auto_recall.recall_top_k") or 5),
        recall_min_score=float(engine.config.get("auto_recall.recall_min_score") or 0.30),
        recall_evidence_min=float(engine.config.get("auto_recall.evidence_min_cosine") or 0.0),
        specificity_strong_cosine=float(
            engine.config.get("auto_recall.specificity_strong_cosine") or 0.0),
        conversational_gap_max=float(
            engine.config.get("auto_recall.conversational_gap_max") or 0.0),
        conversational_conf_max=float(
            engine.config.get("auto_recall.conversational_conf_max") or 0.0),
        query_worthiness_tau=float(
            engine.config.get("auto_recall.query_worthiness_tau") or 0.0),
        surface_decisions=bool(engine.config.get("auto_recall.surface_decisions")),
        correction_capture=bool(
            engine.config.get("auto_recall.correction_capture")
            if engine.config.get("auto_recall.correction_capture") is not None
            else True),
        correction_record_draft=bool(
            engine.config.get("auto_recall.correction_record_draft")
            if engine.config.get("auto_recall.correction_record_draft") is not None
            else True),
        correction_importance=float(
            engine.config.get("auto_recall.correction_importance") or 0.85),
        repeat_guard=bool(
            engine.config.get("auto_recall.repeat_guard")
            if engine.config.get("auto_recall.repeat_guard") is not None
            else True),
    )
    if res.skipped or res.is_empty():
        return None
    cap = min(int(max_chars),
              int(engine.config.get("auto_recall.budget_chars") or 4000))
    text = format_context(
        res, max_chars=cap,
        include_trace=bool(engine.config.get("auto_recall.include_trace")),
    )
    # Memory Delta Protocol: collapse items already shown this session into
    # one-liner handle references. Opt-in via config; silent no-op on error so
    # an inert ledger can never break the hook.
    try:
        if text and bool(engine.config.get("memory_delta.enabled")):
            from pmb.memo.delta_render import apply_delta
            sid = None
            for attr in ("session_id", "_session_id"):
                sid = sid or getattr(engine, attr, None)
            sid = sid or ""
            text = apply_delta(engine, str(sid), res, text)
    except Exception:
        pass
    return text or None
