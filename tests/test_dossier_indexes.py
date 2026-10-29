"""Case-identity lookup must not rescan and decode every historical dossier."""

from radar.store.db import Database


def test_dossier_lookup_uses_root_index_and_preserves_other_sources(db: Database) -> None:
    with db.transaction():
        db.save_dossier_scan("dreal_case_by_case:hdf:1", "https://example.org/case", {"state": "complete"})
        db.save_dossier_scan("mrae_opinion:hdf:2", "https://example.org/case", {"state": "partial"})
        db.save_dossier_scan("dreal_case_by_case:hdf:3", "https://example.org/other", {"state": "complete"})
    assert set(db.dossier_keys_for_root("https://example.org/case")) == {
        "dreal_case_by_case:hdf:1",
        "mrae_opinion:hdf:2",
    }
    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT filing_key FROM dossier_scans WHERE root_url=?", ("https://example.org/case",)
    ).fetchall()
    assert any("dossier_scans_root" in row[3] for row in plan)


def test_dossier_diagnostics_are_region_scoped_and_bounded(db: Database) -> None:
    with db.transaction():
        for i in range(30):
            db.save_dossier_scan(f"dreal_case_by_case:hdf:{i}", f"https://example.org/{i}", {"state": "partial"})
        db.save_dossier_scan("dreal_case_by_case:hdf:complete", "https://example.org/complete", {"state": "complete"})
        db.save_dossier_scan("mrae_opinion:elsewhere:1", "https://example.org/elsewhere", {"state": "blocked"})
    report = db.dossier_summary("hdf")
    assert report["by_state"] == {"partial": 30, "complete": 1}
    assert report["incomplete_n"] == 30 and len(report["incomplete"]) == 20 and report["truncated"]
