import pytest

from scripts import source_reconciliation as audit


@pytest.mark.parametrize("value", [None, "bad", float("nan"), float("inf")])
def test_unknown_amount_cannot_match_zero(value):
    source = dict.fromkeys(audit.METRICS, 0)
    source[audit.METRICS[0]] = value
    snapshot = {"metrics": dict.fromkeys(audit.METRICS, 0), "date": "2026-09-13",
                "period_start": "2026-09-01", "source_task_id": "task"}
    result = audit._compare(source, snapshot, "2026-09-13", "2026-09-01")
    assert result["status"] == "invalid_values"
    assert result["delta_snapshot_minus_source"][audit.METRICS[0]] is None


def test_duplicate_source_and_parse_error_prevent_clean_report(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    import sqlite3
    sqlite3.connect(tmp_path / "data" / "app.db").close()
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "PERIODS", [("2026-09", "2026-09-13")])
    monkeypatch.setattr(audit, "PILOT_STORES", {"store": "shop"})
    monkeypatch.setattr(audit, "_latest_snapshots", lambda *args: {})
    monkeypatch.setattr(audit, "_source_exports", lambda *args: {
        "files": [], "records": {}, "duplicates": {"shop": ["a.xlsx", "b.xlsx"]},
        "errors": [{"path": "broken.xlsx", "error": "invalid"}],
    })
    report = audit.build_report()
    period = report["periods"][0]
    assert period["comparisons"][0]["status"] == "duplicate_source"
    assert period["summary"] == {"pilot_count": 1, "matches": 0, "issues": 2}
    rendered = audit._markdown(report)
    assert "0.00" not in rendered
    assert "待核对" in rendered
    assert "broken.xlsx" in rendered
