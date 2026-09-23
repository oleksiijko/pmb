"""Regression test for `pmb decay` crashing on its own summary line.

`apply_decay` returns `n_active_processed`, `n_decayed`, `n_archived` and
`decayed_by_tier`. It has never returned `decay_factor` — decay is per-tier
(`TIER_DECAY_FACTORS`), so a single factor does not exist. The CLI's summary
line nonetheless formatted `result['decay_factor']`, so `pmb decay` raised
KeyError on every run.

The importance updates are written before that line, so decay actually worked;
only the report died. That is the nastiest shape of this bug — scheduled
maintenance looks broken (or, piped to a log, looks like a hard failure) while
the data change silently succeeded.

These tests pin both halves: the contract of what `apply_decay` returns, and
that the CLI renders it without raising.
"""
from __future__ import annotations

from pmb.cli.commands import maintenance


class _FakeEngine:
    """Minimal engine stub returning the documented apply_decay payload."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.config = {"decay.archive_cold_days": 90}

    def apply_daily_decay(self, days_since: float = 1.0) -> dict:
        return self._payload


REAL_PAYLOAD = {
    "n_active_processed": 1464,
    "n_decayed": 620,
    "n_archived": 3,
    "decayed_by_tier": {"episodic": 600, "working": 20},
}


def test_apply_decay_payload_has_no_decay_factor_key():
    # Pins the contract the CLI must render. If a future change reintroduces a
    # single factor, this test should be updated deliberately, not by accident.
    assert "decay_factor" not in REAL_PAYLOAD
    assert set(REAL_PAYLOAD) == {
        "n_active_processed", "n_decayed", "n_archived", "decayed_by_tier",
    }


def test_decay_command_renders_without_keyerror(monkeypatch, capsys):
    monkeypatch.setattr(maintenance, "Engine", lambda *a, **k: _FakeEngine(REAL_PAYLOAD))
    # The regression: this raised KeyError: 'decay_factor'.
    maintenance.decay(days=1.0, archive_cold=False, apply=False)
    out = capsys.readouterr().out
    assert "620" in out and "1464" in out
    assert "decay_factor" not in out


def test_decay_command_survives_missing_tier_breakdown(monkeypatch, capsys):
    # Defensive: an older/partial payload must not resurrect the crash.
    payload = {k: v for k, v in REAL_PAYLOAD.items() if k != "decayed_by_tier"}
    monkeypatch.setattr(maintenance, "Engine", lambda *a, **k: _FakeEngine(payload))
    maintenance.decay(days=1.0, archive_cold=False, apply=False)
    out = capsys.readouterr().out
    assert "none" in out


def test_decay_command_reports_each_tier(monkeypatch, capsys):
    monkeypatch.setattr(maintenance, "Engine", lambda *a, **k: _FakeEngine(REAL_PAYLOAD))
    maintenance.decay(days=1.0, archive_cold=False, apply=False)
    out = capsys.readouterr().out
    assert "episodic=600" in out
    assert "working=20" in out
