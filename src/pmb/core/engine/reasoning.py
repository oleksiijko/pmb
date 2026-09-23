from __future__ import annotations

import time


class ReasoningMixin:
    def distill_lessons(
        self,
        *,
        session_id: str | None = None,
        backend: str = "auto",
        model: str | None = None,
        dry_run: bool = False,
        llm=None,
    ) -> dict:
        """Auto-distill durable lessons/failures from a session's events via
        an LLM. Off the recall hot path. See pmb.health.distill_lessons."""
        from pmb.health.distill_lessons import distill_lessons as _distill

        return _distill(
            self, session_id=session_id, backend=backend, model=model, dry_run=dry_run, llm=llm
        )

    def rehearse(
        self,
        importance_threshold: float = 0.5,
        min_idle_days: float = 7.0,
        max_rehearse: int = 20,
    ) -> dict:
        """Spaced-repetition rehearsal - keep important-but-idle memories alive."""
        from pmb.health.rehearse import rehearse as _rehearse

        return _rehearse(
            self,
            importance_threshold=importance_threshold,
            min_idle_days=min_idle_days,
            max_rehearse=max_rehearse,
        ).to_dict()

    def consolidate(
        self,
        dry_run: bool = False,
        backend: str | None = None,
        model: str | None = None,
        since_days: float = 14.0,
        similarity_threshold: float = 0.5,
        min_cluster_size: int = 3,
        max_clusters: int = 10,
        llm=None,
    ) -> dict:
        """
        LLM-based generalization: cluster recent events by embedding similarity,
        ask the LLM to extract a rule per cluster, store as a fact, archive sources.

        backend: "auto" picks Claude CLI, then Anthropic, then OpenAI, then
        Ollama. "claude" / "anthropic" / "openai" / "ollama" force a choice.

        Pass an `llm` instance with .consolidate(texts) directly for tests.
        """
        from pmb.health.consolidate import run_consolidation

        result = run_consolidation(
            self,
            llm=llm,
            backend=backend or self.config.get("consolidate.backend"),
            model=model,
            since_days=since_days,
            similarity_threshold=similarity_threshold,
            min_cluster_size=min_cluster_size,
            max_clusters=max_clusters,
            dry_run=dry_run,
        )
        if not dry_run and not result.get("n_failed", 0):
            try:
                from pmb.health.auto_consolidate import mark_consolidation_done

                mark_consolidation_done(self)
            except Exception:
                pass
        return result

    def consolidation_due(self) -> dict:
        """Inspect whether auto-trigger thresholds are met right now."""
        from pmb.health.auto_consolidate import should_trigger

        return should_trigger(self)

    def reflect_event(
        self,
        ulid: str,
        llm=None,
        context_size: int = 4,
        backend: str = "auto",
    ) -> dict | None:
        """Run LLM reflection on a single event. Stores a 'reflection'-typed
        event linking back to the source. Returns the reflection dict, or
        None if LLM unavailable / parsing failed / source not found.

        Reflections are searchable like any other event, which is how
        multi-hop questions get bridged at recall time without any read-
        time LLM call.

        Safe to call repeatedly - duplicate reflections for the same source
        are prevented by checking metadata.source_ulid.
        """
        ev = self.events.get_by_ulid(ulid)
        if not ev or ev.event_type == "reflection":
            return None
        # Skip if already reflected
        if self._has_reflection_for(ulid):
            return None

        if llm is None:
            from pmb.health.consolidate import resolve_llm_client

            try:
                llm = resolve_llm_client(backend=backend)
            except Exception as e:
                import logging

                logging.getLogger(__name__).info("No LLM for reflection: %s", e)
                return None

        from pmb.reasoning.reflect import Reflector

        reflector = Reflector(llm, max_context_events=context_size)

        # Pull a small temporal context window (events near this one in time
        # AND/OR sharing entities). This is what makes causation hints in
        # the reflection more accurate.
        context = self._reflection_context(ev, max_n=context_size)

        reflection = reflector.reflect(ev, context_events=context)
        if reflection is None:
            return None

        # Persist as a new event
        from pmb.core.events import TIER_SEMANTIC, Event

        new_ev = Event(
            event_type="reflection",
            content=reflection.to_event_content(),
            metadata=reflection.to_metadata(),
            workspace_id=self.workspace.id,
            importance=0.6,  # reflections are useful by default
            tier=TIER_SEMANTIC,  # they capture stable understanding
        )
        appended = self.events.append(new_ev)
        # Index in graph too - reflections contain rich entity signal
        try:
            self._index_event_in_graph(appended, full_text=appended.content)
        except Exception:
            pass
        # Add to vector index for hybrid recall
        try:
            self.search.add(appended.ulid, appended.to_text())
        except Exception:
            pass
        # Phase B (Improvement B): ALSO link reflection-derived entities
        # back to the SOURCE event. This makes the source findable via
        # entity matches against terms the LLM surfaced (people names,
        # themes, implications). PPR naturally walks these enriched links.
        # No new searchable chunk needed - pure graph signal.
        n_bridge_entities = 0
        if self.config.get("recall.reflection_to_edges"):
            try:
                refl_full_text = (
                    reflection.significance
                    + " "
                    + " ".join(reflection.implications)
                    + " "
                    + " ".join(reflection.might_answer)
                    + " "
                    + " ".join(reflection.linked_themes)
                    + " "
                    + " ".join(reflection.people)
                )
                refl_ext = self.entity_extractor.extract(refl_full_text)
                refl_named = refl_ext.all_named()
                # Add LLM-extracted people as 'person' entities too - our
                # rule-based extractor doesn't catch capitalized names.
                for p in reflection.people:
                    if p and len(p) >= 2:
                        refl_named.append(("person", p.lower()))
                # Add themes as concepts (deduplicate later)
                for th in reflection.linked_themes:
                    if th and len(th) >= 3:
                        refl_named.append(("theme", th.lower()))

                src_entity_ids: list[int] = []
                seen_kn = set()
                for kind, name in refl_named:
                    key = (kind, name)
                    if key in seen_kn:
                        continue
                    seen_kn.add(key)
                    eid = self.graph.upsert_entity(self.workspace.id, kind, name)
                    src_entity_ids.append(eid)
                if src_entity_ids:
                    # Link to SOURCE ulid (not the reflection event)
                    self.graph.link_event(ulid, src_entity_ids)
                    # Bump co-occurrence among these new entities
                    self.graph.bump_edges(self.workspace.id, src_entity_ids)
                    n_bridge_entities = len(src_entity_ids)
            except Exception:
                pass
        # Invalidate recall cache so future queries see the new reflection
        self.recall_cache.bump_generation()
        return {
            "source_ulid": ulid,
            "reflection_ulid": appended.ulid,
            "significance": reflection.significance,
            "might_answer": reflection.might_answer,
            "themes": reflection.linked_themes,
            "bridge_entities_added_to_source": n_bridge_entities,
        }

    def precompute_predictive_cache(
        self,
        n_questions: int = 15,
        events_to_consider: int = 25,
        cache_top_k: int = 20,
        llm=None,
        backend: str = "auto",
    ) -> dict:
        """LLM generates likely user questions; for each we pre-compute
        and cache top-K. At read time, fuzzy match on query embedding
        returns cached results instantly.

        Run during sleep / idle. Refreshes the cache. Old entries (over
        TTL) ignored at read time but kept until clear_predictive_cache().
        """
        # Pull recent active events (raw + facts + reflections all OK)
        import sqlite3

        with sqlite3.connect(self.workspace.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ulid FROM events WHERE workspace_id = ? "
                "AND archived_at IS NULL "
                "ORDER BY timestamp DESC LIMIT ?",
                (self.workspace.id, events_to_consider),
            ).fetchall()
        candidates = [self.events.get_by_ulid(r["ulid"]) for r in rows]
        candidates = [c for c in candidates if c is not None]
        if not candidates:
            return {"n_questions_generated": 0, "n_cached": 0, "skipped": "no_events"}

        if llm is None:
            from pmb.health.consolidate import resolve_llm_client

            try:
                llm = resolve_llm_client(backend=backend)
            except Exception:
                return {
                    "n_questions_generated": 0,
                    "n_cached": 0,
                    "skipped": "no_llm",
                }

        from pmb.reasoning.predictive import (
            CacheEntry,
            PredictiveQuestionGenerator,
            store_entry,
        )

        gen = PredictiveQuestionGenerator(
            llm,
            max_questions=n_questions,
            max_events_in_prompt=events_to_consider,
        )
        questions = gen.generate(candidates)
        if not questions:
            return {"n_questions_generated": 0, "n_cached": 0, "skipped": "llm_returned_empty"}

        n_cached = 0
        for q in questions:
            try:
                # Run full recall on this question (with predictive disabled to
                # avoid infinite recursion / self-lookup)
                pack = self.recall(
                    q,
                    top_k=cache_top_k,
                    _skip_decompose=True,
                )
                top_ulids = [r.ulid for r in pack.results]
                if not top_ulids:
                    continue
                emb = self.search.embed(q)
                store_entry(
                    self.workspace.db_path,
                    CacheEntry(
                        id=None,
                        workspace_id=self.workspace.id,
                        query_text=q,
                        query_embedding=emb,
                        top_ulids=top_ulids,
                        created_at=time.time(),
                    ),
                )
                n_cached += 1
            except Exception:
                continue
        return {
            "n_questions_generated": len(questions),
            "n_cached": n_cached,
            "events_considered": len(candidates),
        }

    def clear_predictive_cache(self) -> int:
        from pmb.reasoning.predictive import clear_cache

        n = clear_cache(self.workspace.db_path, self.workspace.id)
        return n

    def extract_facts_batch(
        self,
        limit: int = 50,
        max_age_days: float = 365.0,
        llm=None,
        backend: str = "auto",
        max_facts_per_event: int = 30,
    ) -> dict:
        """Improvement D: extract atomic facts from recent events.

        Each fact becomes a new event of event_type='fact_atom' linked to
        its source by metadata.source_ulid. Facts are searchable like any
        event, so they surface in recall and give the reader pre-extracted
        clean statements.

        Skips events that already have facts (idempotent).
        """
        candidates = self._un_factualized_events(limit=limit, max_age_days=max_age_days)
        if not candidates:
            return {
                "n_candidates": 0,
                "n_facts_added": 0,
                "n_failed": 0,
            }
        if llm is None:
            from pmb.health.consolidate import resolve_llm_client

            try:
                llm = resolve_llm_client(backend=backend)
            except Exception:
                return {
                    "n_candidates": len(candidates),
                    "skipped": "no_llm",
                    "n_facts_added": 0,
                    "n_failed": 0,
                }

        from pmb.core.events import TIER_SEMANTIC, Event
        from pmb.reasoning.facts import FactExtractor

        extractor = FactExtractor(llm, max_facts_per_event=max_facts_per_event)
        n_facts = 0
        n_failed = 0
        for ev in candidates:
            try:
                facts = extractor.extract(ev)
                for f in facts:
                    fact_ev = Event(
                        event_type="fact_atom",
                        content=f.to_event_content(),
                        metadata={
                            "source_ulid": f.source_ulid,
                            "kind": "atomic_fact",
                        },
                        workspace_id=self.workspace.id,
                        importance=0.7,  # atomic facts are valuable signal
                        tier=TIER_SEMANTIC,
                    )
                    appended = self.events.append(fact_ev)
                    try:
                        self._index_event_in_graph(appended, full_text=appended.content)
                    except Exception:
                        pass
                    try:
                        self.search.add(appended.ulid, appended.to_text())
                    except Exception:
                        pass
                    n_facts += 1
            except Exception:
                n_failed += 1
        self.recall_cache.bump_generation()
        return {
            "n_candidates": len(candidates),
            "n_facts_added": n_facts,
            "n_failed": n_failed,
        }

    def _un_factualized_events(self, limit: int, max_age_days: float):
        """Events that don't yet have fact_atom children."""
        import sqlite3

        cutoff = time.time() - max_age_days * 86400
        with sqlite3.connect(self.workspace.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ulid FROM events e
                WHERE workspace_id = ?
                  AND event_type NOT IN ('fact_atom', 'reflection')
                  AND archived_at IS NULL
                  AND timestamp >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM events f
                      WHERE f.workspace_id = e.workspace_id
                        AND f.event_type = 'fact_atom'
                        AND json_extract(f.metadata_json, '$.source_ulid') = e.ulid
                  )
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (self.workspace.id, cutoff, limit),
            ).fetchall()
            ulids = [r["ulid"] for r in rows]
        return [self.events.get_by_ulid(u) for u in ulids if self.events.get_by_ulid(u)]

    def reflect_batch(
        self,
        limit: int = 20,
        max_age_days: float = 30.0,
        llm=None,
        backend: str = "auto",
        context_size: int = 4,
        extract_causation: bool = True,
    ) -> dict:
        """Reflect on up to `limit` recent events that haven't been
        reflected on yet. Designed to run in the background during sleep /
        consolidation cycles, not in any hot path.

        Returns a summary dict with counts and a sample of generated
        reflections (truncated). Failures are counted, not raised.
        """
        candidates = self._unreflected_events(limit=limit, max_age_days=max_age_days)
        if not candidates:
            return {"n_candidates": 0, "n_reflected": 0, "n_failed": 0, "samples": []}

        # Resolve LLM once for the whole batch
        if llm is None:
            from pmb.health.consolidate import resolve_llm_client

            try:
                llm = resolve_llm_client(backend=backend)
            except Exception:
                return {
                    "n_candidates": len(candidates),
                    "n_reflected": 0,
                    "n_failed": 0,
                    "skipped": "no_llm",
                    "samples": [],
                }

        n_ok = 0
        n_fail = 0
        n_edges = 0
        samples: list[dict] = []

        # Build extractor lazily, share across batch
        causation_extractor = None
        if extract_causation:
            try:
                from pmb.reasoning.causation import CausationExtractor

                causation_extractor = CausationExtractor(llm)
            except Exception:
                causation_extractor = None

        for ev in candidates:
            try:
                out = self.reflect_event(
                    ev.ulid,
                    llm=llm,
                    context_size=context_size,
                )
                if out:
                    n_ok += 1
                    if len(samples) < 3:
                        samples.append(
                            {
                                "source_ulid": ev.ulid,
                                "source_preview": (ev.content or "")[:120],
                                "significance": out["significance"][:160],
                                "might_answer": out["might_answer"][:3],
                            }
                        )
                    # Bonus: extract causation edges from this event to recent
                    # context. Same context window we used for reflection.
                    if causation_extractor:
                        try:
                            ctx = self._reflection_context(ev, max_n=context_size)
                            edges = causation_extractor.extract(ev, ctx)
                            from pmb.reasoning.causation import upsert_edge

                            for e in edges:
                                upsert_edge(self.workspace.db_path, e)
                                n_edges += 1
                        except Exception:
                            pass
                else:
                    n_fail += 1
            except Exception:
                n_fail += 1
        return {
            "n_candidates": len(candidates),
            "n_reflected": n_ok,
            "n_failed": n_fail,
            "n_causation_edges": n_edges,
            "samples": samples,
        }

    def cluster_events_into_arcs(
        self,
        limit: int = 20,
        max_age_days: float = 60.0,
        llm=None,
        backend: str = "auto",
        refresh_summaries: bool = True,
    ) -> dict:
        """LLM clusters recent unassigned events into narrative arcs.

        For each candidate event:
          - Show LLM the existing active arcs + this event
          - LLM decides: join arc, create new arc, or ignore
          - Persist accordingly

        After clustering, optionally rewrite arc summaries to reflect new
        members (one LLM call per touched arc).
        """
        from pmb.reasoning.arcs import (
            Arc,
            ArcManager,
            add_event_to_arc,
            create_arc,
            events_in_arc,
            get_arc,
            list_arcs,
            update_arc,
        )

        # Pull recent unassigned events
        candidates = self._unassigned_events(limit=limit, max_age_days=max_age_days)
        if not candidates:
            return {"n_candidates": 0, "n_joined": 0, "n_created": 0, "n_ignored": 0}

        if llm is None:
            from pmb.health.consolidate import resolve_llm_client

            try:
                llm = resolve_llm_client(backend=backend)
            except Exception:
                return {
                    "n_candidates": len(candidates),
                    "skipped": "no_llm",
                    "n_joined": 0,
                    "n_created": 0,
                    "n_ignored": 0,
                }
        mgr = ArcManager(llm)

        touched_arcs: set[int] = set()
        n_joined = n_created = n_ignored = 0
        for ev in candidates:
            arcs_now = list_arcs(self.workspace.db_path, self.workspace.id, status="active")
            decision = mgr.assign_event(ev, arcs_now, self.workspace.id)
            action = decision.get("action")
            if action == "join":
                arc_id = decision.get("arc_id")
                # Make sure the arc actually exists in our workspace
                if get_arc(self.workspace.db_path, arc_id):
                    if add_event_to_arc(self.workspace.db_path, arc_id, ev.ulid):
                        touched_arcs.add(arc_id)
                        n_joined += 1
                else:
                    n_ignored += 1
            elif action == "create":
                new_arc = Arc(
                    id=None,
                    workspace_id=self.workspace.id,
                    title=decision["new_title"],
                    summary="",
                    first_event_ulid=ev.ulid,
                    last_event_ulid=ev.ulid,
                    n_events=0,  # set by add_event_to_arc
                )
                arc_id = create_arc(self.workspace.db_path, new_arc)
                add_event_to_arc(self.workspace.db_path, arc_id, ev.ulid)
                touched_arcs.add(arc_id)
                n_created += 1
            else:
                n_ignored += 1

        # Refresh summaries for touched arcs
        n_summaries = 0
        if refresh_summaries and touched_arcs:
            for aid in touched_arcs:
                arc = get_arc(self.workspace.db_path, aid)
                if not arc:
                    continue
                event_ulids = events_in_arc(self.workspace.db_path, aid)
                events = [self.events.get_by_ulid(u) for u in event_ulids]
                events = [e for e in events if e is not None]
                events.sort(key=lambda e: e.timestamp)
                if not events:
                    continue
                try:
                    summary = mgr.write_summary(arc, events)
                    update_arc(self.workspace.db_path, aid, summary=summary)
                    n_summaries += 1
                except Exception:
                    pass

        # Cache invalidation - new arcs / summaries affect recall
        self.recall_cache.bump_generation()

        return {
            "n_candidates": len(candidates),
            "n_joined": n_joined,
            "n_created": n_created,
            "n_ignored": n_ignored,
            "n_summaries_updated": n_summaries,
        }

    def list_arcs(
        self,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[dict]:
        from pmb.reasoning.arcs import list_arcs

        return [
            a.to_dict()
            for a in list_arcs(
                self.workspace.db_path,
                self.workspace.id,
                status=status,
                limit=limit,
            )
        ]

    def arc_detail(self, arc_id: int) -> dict | None:
        from pmb.reasoning.arcs import events_in_arc, get_arc

        arc = get_arc(self.workspace.db_path, arc_id)
        if not arc or arc.workspace_id != self.workspace.id:
            return None
        ev_ulids = events_in_arc(self.workspace.db_path, arc_id)
        events = [self.events.get_by_ulid(u) for u in ev_ulids]
        events = [e for e in events if e is not None]
        events.sort(key=lambda e: e.timestamp)
        return {
            **arc.to_dict(),
            "events": [
                {
                    "ulid": e.ulid,
                    "timestamp": e.timestamp,
                    "preview": (e.content or "")[:200],
                }
                for e in events
            ],
        }

    def _unassigned_events(self, limit: int = 20, max_age_days: float = 60.0):
        """Recent non-reflection events that aren't in any arc yet."""
        import sqlite3

        cutoff = time.time() - max_age_days * 86400
        with sqlite3.connect(self.workspace.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ulid FROM events e
                WHERE workspace_id = ?
                  AND event_type != 'reflection'
                  AND archived_at IS NULL
                  AND timestamp >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM arc_events ae WHERE ae.event_ulid = e.ulid
                  )
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (self.workspace.id, cutoff, limit),
            ).fetchall()
            ulids = [r["ulid"] for r in rows]
        return [self.events.get_by_ulid(u) for u in ulids if self.events.get_by_ulid(u)]

    def _has_reflection_for(self, source_ulid: str) -> bool:
        import sqlite3

        with sqlite3.connect(self.workspace.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE workspace_id = ? AND event_type = 'reflection' "
                "AND json_extract(metadata_json, '$.source_ulid') = ? LIMIT 1",
                (self.workspace.id, source_ulid),
            ).fetchone()
            return row is not None

    def _unreflected_events(
        self,
        limit: int = 20,
        max_age_days: float = 30.0,
    ):
        """Recent non-reflection events that don't yet have a reflection."""
        import sqlite3

        cutoff = time.time() - max_age_days * 86400
        with sqlite3.connect(self.workspace.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ulid FROM events e
                WHERE workspace_id = ?
                  AND event_type != 'reflection'
                  AND archived_at IS NULL
                  AND timestamp >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM events r
                      WHERE r.workspace_id = e.workspace_id
                        AND r.event_type = 'reflection'
                        AND json_extract(r.metadata_json, '$.source_ulid') = e.ulid
                  )
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (self.workspace.id, cutoff, limit),
            ).fetchall()
            ulids = [r["ulid"] for r in rows]
        return [self.events.get_by_ulid(u) for u in ulids if self.events.get_by_ulid(u)]

    def _reflection_context(self, ev, max_n: int = 4):
        """Pick a small set of events to give the reflection LLM context.

        Combines temporal nearest neighbours (events written shortly before
        this one) with entity-sharing neighbours from the graph. Both
        signals are cheap and give the LLM useful surrounding context.
        """
        import sqlite3

        ctx: list = []
        seen: set[str] = {ev.ulid}
        # Temporal window: 6 events immediately before this one
        with sqlite3.connect(self.workspace.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ulid FROM events WHERE workspace_id = ? AND archived_at IS NULL "
                "AND event_type != 'reflection' AND timestamp < ? "
                "ORDER BY timestamp DESC LIMIT 6",
                (self.workspace.id, ev.timestamp),
            ).fetchall()
        for r in rows:
            u = r["ulid"]
            if u in seen:
                continue
            seen.add(u)
            full = self.events.get_by_ulid(u)
            if full:
                ctx.append(full)
            if len(ctx) >= max_n:
                break
        return ctx

    def maybe_auto_consolidate(self, **kwargs) -> dict | None:
        """Run consolidation only if auto-trigger thresholds are met.

        Returns the consolidation result if a run happened, else None.
        Safe to call from any quiet moment (CLI exit hook, MCP idle, cron).
        """
        decision = self.consolidation_due()
        if not decision.get("should_run"):
            return None
        return self.consolidate(**kwargs)
