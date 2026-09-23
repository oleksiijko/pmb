# Roadmap

Honest and local-first. PMB is a personal tool: every item below keeps memory on
your disk, offline by default, with no call-home. Dates are intentionally absent -
this is a one-maintainer project; order signals priority, not a schedule.

## Shipped - v0.9.0 "Anchor Engine" (2026-06)

Language packs are no longer required. RU/UK `packs/ru.yaml` + `packs/uk.yaml`
were **deleted**; many languages now ride one mechanism instead of a pack each:

- **Semantic anchors** - English exemplars classify intents + keyed-fact
  extraction; the multilingual embedder transfers them cross-lingually, so a
  query in any language the model knows lands on the right anchor.
- **ALD (anchor→lexicon distillation)** - the cold lexical path self-compiles
  from your own traffic into `$PMB_HOME/lang/auto.yaml`; a language you use gets
  faster over time with zero configuration.
- **Measured** - V1 RU/UK recall top-1 = 1.00 (byte-identical to the pack era);
  101-query multilingual eval overall top-3 ≈ 0.91; anchor classify p95 ≈ 81 ms.
  A blocking CI gate runs the eval with packs off so recall can't silently
  regress. See [Adding a language](contributing/adding-a-language.md).

## Prepared — v1.3.0 "Reliable local memory"

- Verified SQLite snapshots and recovery, including checksums and safety copies.
- Saved Ollama model/endpoint selection used consistently by maintenance.
- Shared embedding inference protected against concurrent native-runtime crashes.
- Reproducible CI dependency constraints and complete local test entry points.

The release candidate must pass the full CI matrix before publication.
See [project assessment](PROJECT_REVIEW.md) for follow-up priorities.

## Next

- **Validate ALD on real traffic.** The distillation loop is proven in tests; the
  open question is how fast it self-heals a language's cold path in real daily
  use. Add a metric (cold-path coverage over time) and report it honestly.
- **Latency on commodity hardware.** The current SLO budget was relaxed for a
  memory-constrained dev box. Re-measure recall + anchor p95 on a normal machine
  and tighten the gate to the real number.
- **Decide on default-on keyed extraction.** The v0.9 hypothesis-margin keyed
  extraction (C2/F1/F2) ships **default-off** pending real-world precision data;
  promote to default only once the false-positive rate is measured in the field.
- **Stronger embedder path.** Make `bge-m3` a first-class, documented upgrade for
  CJK / lower-resource languages (anchor calibration already rebuilds per model).
- **CJK + cross-lingual top-1.** Exact top-1 is weak for zh/ja and cross-lingual
  queries (top-3 stays strong); investigate rerank or a CJK-aware tokenizer for
  the cold path.

## Ingest + storage

- **Backup** via litestream (continuous SQLite replication to a bucket you own).
- **Optional cloud-sync**, bring-your-own bucket - never a PMB-hosted service.
- **tree-sitter** project indexing for Rust / TypeScript (today's deep indexer is
  Python-first; other languages fall back to a lighter scan).
- **Image OCR** so screenshots and scanned PDFs become searchable memory.

## Non-goals

- No hosted PMB service, no telemetry, no call-home - there is nothing to add
  later because there is no server.
- No silent network calls on the read or write path. Optional LLM passes
  (`consolidate`, LLM synthesizers) stay explicit and opt-in.

Want something prioritized? Open a discussion on GitHub.
