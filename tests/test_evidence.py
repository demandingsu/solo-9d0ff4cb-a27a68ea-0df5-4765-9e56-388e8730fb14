"""Tests for the point-evidence audit API.

Covers the emission-accounting contract:

* active and retracted event ids are both queryable;
* a retracted id keeps the exact points it was published with, while the
  replacement id includes the late points;
* identical re-uploads never mutate a frozen evidence snapshot;
* keyset pagination by point id is complete (no dup, no gap) and its total
  equals the event's point_count, including while another instance uploads;
* unknown ids and wrong-port ids return an explicit 404 error.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import csv_ingest, db, service
from app.config import Settings
from app.main import create_app

PORT = {
    "code": "CNSHA-PD",
    "name": "上海港浦东",
    "min_lat": 31.0,
    "max_lat": 31.4,
    "min_lon": 121.2,
    "max_lon": 121.8,
}
INSIDE = (31.2, 121.5)


def utc(hour: int, minute: int = 0, second: int = 0, day: int = 1) -> str:
    return f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def csv_bytes(rows: list[tuple[str, str, float, float]]) -> bytes:
    lines = ["vessel,time,lat,lon"]
    lines += [f"{v},{t},{lat},{lon}" for v, t, lat, lon in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        db_path=str(tmp_path / "test.sqlite3"), demo_enabled=True
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def registered(client: TestClient) -> TestClient:
    response = client.post("/ports", json=PORT)
    assert response.status_code == 201, response.text
    return client


def upload(
    client: TestClient,
    rows: list[tuple[str, str, float, float]],
    *,
    key: str | None = None,
):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        f"/ports/{PORT['code']}/uploads",
        files={"file": ("ais.csv", csv_bytes(rows), "text/csv")},
        headers=headers,
    )


def evidence(
    client: TestClient,
    event_id: int,
    *,
    after: int = 0,
    limit: int = 1000,
    port: str = PORT["code"],
):
    return client.get(
        f"/ports/{port}/events/{event_id}/evidence",
        params={"after_point_id": after, "limit": limit},
    )


def evidence_all(client: TestClient, event_id: int, *, page_size: int = 2):
    """Page through every evidence row and return the collected payload."""
    seen: list[dict] = []
    cursor = 0
    first_body = None
    while True:
        response = evidence(client, event_id, after=cursor, limit=page_size)
        assert response.status_code == 200, response.text
        body = response.json()
        if first_body is None:
            first_body = body
        seen.extend(body["points"])
        if not body["has_more"]:
            return first_body, seen
        cursor = body["next_cursor"]


def seed_single_event(
    client: TestClient, *, vessel: str = "SHIP-A", key: str = "seed"
) -> dict:
    rows = [(vessel, utc(0, m), *INSIDE) for m in (0, 10, 20, 30)]
    response = upload(client, rows, key=key)
    assert response.status_code == 201, response.text
    event = client.get(f"/ports/{PORT['code']}/events").json()["events"][0]
    return event


def test_active_event_evidence_returns_points_and_header(
    registered: TestClient,
) -> None:
    event = seed_single_event(registered)
    response = evidence(registered, event["event_id"])
    assert response.status_code == 200, response.text
    body = response.json()

    header = body["event"]
    assert header["event_id"] == event["event_id"]
    assert header["status"] == "active"
    assert header["vessel_id"] == "SHIP-A"
    assert header["start_time"] == event["start_time"]
    assert header["end_time"] == event["end_time"]
    assert header["point_count"] == 4
    assert header["start_time_utc"].endswith("Z")
    assert header["end_time_utc"].endswith("Z")
    assert header["published_batch_id"] >= 1

    assert body["evidence_count"] == 4
    assert body["count"] == 4
    assert body["has_more"] is False
    point_ids = [point["point_id"] for point in body["points"]]
    assert point_ids == sorted(point_ids)
    for point in body["points"]:
        assert point["vessel_id"] == "SHIP-A"
        assert isinstance(point["event_time"], int)
        assert point["event_time_utc"].endswith("Z")
        assert point["lat"] == INSIDE[0]
        assert point["lon"] == INSIDE[1]
        assert isinstance(point["upload_batch_id"], int)
        assert point["upload_batch_received_at"]


def test_evidence_count_always_equals_event_point_count(
    registered: TestClient,
) -> None:
    # A longer stay with 9 points; verify the frozen total matches the event.
    rows = [
        ("SHIP-A", utc(h, m), *INSIDE)
        for h, m in (
            (0, 0), (0, 10), (0, 20), (0, 30), (0, 40), (0, 50),
            (1, 0), (1, 10), (1, 20),
        )
    ]
    upload(registered, rows, key="long")
    event = registered.get(f"/ports/{PORT['code']}/events").json()["events"][0]
    body, points = evidence_all(registered, event["event_id"], page_size=3)
    assert body["evidence_count"] == event["point_count"] == 9
    assert len(points) == 9


def test_retracted_event_keeps_publication_snapshot_and_new_id_adds_late_points(
    registered: TestClient,
) -> None:
    event = seed_single_event(registered, key="base")
    old_id = event["event_id"]

    late = [
        ("SHIP-A", "2025-12-31T23:50:00Z", *INSIDE),
        ("SHIP-A", "2025-12-31T23:55:00Z", *INSIDE),
    ]
    late_response = upload(registered, late, key="late")
    assert late_response.status_code == 201

    active = registered.get(f"/ports/{PORT['code']}/events").json()["events"]
    assert len(active) == 1
    new_id = active[0]["event_id"]
    assert new_id != old_id
    assert active[0]["point_count"] == 6

    # The retracted id is still queryable and answers for only the four
    # points it was published with.
    old_body = evidence(registered, old_id).json()
    assert old_body["event"]["status"] == "retracted"
    assert old_body["event"]["point_count"] == 4
    assert old_body["evidence_count"] == 4
    old_points = old_body["points"]
    assert all(p["vessel_id"] == "SHIP-A" for p in old_points)
    old_times = {p["event_time"] for p in old_points}
    late_epochs = {
        int(datetime(2025, 12, 31, 23, 50, tzinfo=timezone.utc).timestamp()),
        int(datetime(2025, 12, 31, 23, 55, tzinfo=timezone.utc).timestamp()),
    }
    assert old_times.isdisjoint(late_epochs)

    # The replacement id has its own snapshot containing the late points.
    new_body = evidence(registered, new_id).json()
    assert new_body["event"]["status"] == "active"
    assert new_body["evidence_count"] == 6
    new_times = {p["event_time"] for p in new_body["points"]}
    assert late_epochs.issubset(new_times)
    assert old_times.issubset(new_times)

    # The raw point rows are immutable: the old snapshot is a strict subset of
    # the new one (points 1..4 stay), and only the new id contains the two
    # late points.  The old id never gains them.
    old_ids = {p["point_id"] for p in old_body["points"]}
    new_ids = {p["point_id"] for p in new_body["points"]}
    assert old_ids < new_ids
    assert new_ids - old_ids  # the late points belong only to the new id


def test_merged_event_evidence_contains_every_bridge_point(
    registered: TestClient,
) -> None:
    # Two stays separated by a 20 minute gap, then a late point bridges them.
    rows = [("SHIP-A", utc(0, m), *INSIDE) for m in (0, 10, 20, 30)]
    rows += [("SHIP-A", utc(0, 50), *INSIDE)]
    rows += [("SHIP-A", utc(1, m), *INSIDE) for m in (0, 10, 20)]
    upload(registered, rows, key="base2")
    before = registered.get(f"/ports/{PORT['code']}/events").json()["events"]
    assert len(before) == 2
    old_ids = {e["event_id"] for e in before}

    upload(registered, [("SHIP-A", utc(0, 40), *INSIDE)], key="bridge")
    merged = registered.get(f"/ports/{PORT['code']}/events").json()["events"]
    assert len(merged) == 1
    merged_id = merged[0]["event_id"]
    assert merged_id not in old_ids

    body = evidence(registered, merged_id).json()
    # 8 original points + the bridging point = 9, matching point_count.
    assert body["evidence_count"] == merged[0]["point_count"] == 9
    bridge_epoch = int(
        datetime(2026, 1, 1, 0, 40, tzinfo=timezone.utc).timestamp()
    )
    assert bridge_epoch in {p["event_time"] for p in body["points"]}

    # Both predecessor ids were retracted; each keeps its own frozen snapshot.
    for old_id in old_ids:
        old_body = evidence(registered, old_id).json()
        assert old_body["event"]["status"] == "retracted"
        assert old_body["evidence_count"] == old_body["event"]["point_count"]
        assert bridge_epoch not in {
            p["event_time"] for p in old_body["points"]
        }


def test_identical_reupload_does_not_rewrite_frozen_evidence(
    registered: TestClient,
) -> None:
    event = seed_single_event(registered, key="base")
    before = evidence(registered, event["event_id"]).json()

    # Re-upload the exact same observations (deduped, no new raw points) and a
    # second batch whose only rows duplicate stored ones.
    rows = [("SHIP-A", utc(0, m), *INSIDE) for m in (0, 10, 20, 30)]
    replay = upload(registered, rows, key="base")
    assert replay.status_code == 200
    dup_only = upload(registered, rows[:2], key="dup-only")
    assert dup_only.status_code == 201
    assert dup_only.json()["rows_new"] == 0

    after = evidence(registered, event["event_id"]).json()
    assert after["evidence_count"] == before["evidence_count"] == 4
    assert [p["point_id"] for p in after["points"]] == [
        p["point_id"] for p in before["points"]
    ]
    # Every evidence row still points at the original upload batch.
    assert {p["upload_batch_id"] for p in after["points"]} == {
        before["event"]["published_batch_id"]
    }


def test_evidence_pagination_has_no_duplicates_or_gaps(
    registered: TestClient,
) -> None:
    event = seed_single_event(registered)
    body, points = evidence_all(registered, event["event_id"], page_size=2)
    ids = [p["point_id"] for p in points]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)) == body["evidence_count"] == 4

    # Page boundaries: first page says has_more and exposes a resumable cursor.
    first = evidence(registered, event["event_id"], limit=2).json()
    assert first["has_more"] is True
    assert len(first["points"]) == 2
    assert first["next_cursor"] == first["points"][-1]["point_id"]
    second = evidence(
        registered, event["event_id"], after=first["next_cursor"], limit=2
    ).json()
    assert second["has_more"] is False
    assert len(second["points"]) == 2
    # Strictly forward: no overlap between pages.
    assert {p["point_id"] for p in first["points"]}.isdisjoint(
        p["point_id"] for p in second["points"]
    )


def test_pagination_stays_stable_while_another_instance_uploads(
    tmp_path: Path,
) -> None:
    """Concurrent uploads during paging must not duplicate or lose evidence."""
    path = str(tmp_path / "concurrent.sqlite3")
    db.initialise(path)
    setup = db.connect(path)
    try:
        service.register_port(
            setup,
            code=PORT["code"],
            name=PORT["name"],
            min_lat_e6=31_000_000,
            max_lat_e6=31_400_000,
            min_lon_e6=121_200_000,
            max_lon_e6=121_800_000,
        )
        base_rows = [
            csv_ingest.RawPointRow(
                i, "SHIP-A", 1_800_000_000 + i * 600, 31_200_000, 121_500_000
            )
            for i in range(4)
        ]
        service.ingest(
            setup,
            port_code=PORT["code"],
            upload_key="base",
            content=b"base",
            rows=base_rows,
        )
        event_id, _ = service.list_events(setup, port_code=PORT["code"])
        event_id = event_id[0]["event_id"]
    finally:
        setup.close()

    reader = db.connect(path)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        # Keep uploading brand-new points for a *different* vessel, plus
        # duplicate re-uploads for SHIP-A, until the reader has finished.
        index = 0
        while not stop.is_set():
            conn = db.connect(path)
            try:
                with db.write_transaction(conn):
                    row = csv_ingest.RawPointRow(
                        1, "SHIP-B", 1_800_000_000 + index * 600,
                        31_200_000, 121_500_000,
                    )
                    service.ingest(
                        conn,
                        port_code=PORT["code"],
                        upload_key=f"noise-{index}",
                        content=f"noise-{index}".encode(),
                        rows=[row],
                    )
                    duplicate = csv_ingest.RawPointRow(
                        1, "SHIP-A", 1_800_000_000, 31_200_000, 121_500_000
                    )
                    service.ingest(
                        conn,
                        port_code=PORT["code"],
                        upload_key=f"dup-{index}",
                        content=f"dup-{index}".encode(),
                        rows=[duplicate],
                    )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                conn.close()
            index += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        collected: list[int] = []
        cursor = 0
        pages = 0
        while True:
            page = service.get_event_evidence(
                reader,
                port_code=PORT["code"],
                event_id=event_id,
                after_point_id=cursor,
                limit=1,  # deliberately tiny pages
            )
            collected.extend(p["point_id"] for p in page["points"])
            pages += 1
            if not page["has_more"]:
                total = page["evidence_count"]
                break
            cursor = page["next_cursor"]
    finally:
        stop.set()
        thread.join(timeout=30)
        reader.close()

    assert not errors, errors
    assert pages == 4
    assert len(collected) == len(set(collected)) == total == 4

    # The noisy uploads did not retroactively change the frozen snapshot, and
    # the event's point_count still agrees with its evidence total.
    checker = db.connect(path)
    try:
        again = service.get_event_evidence(
            checker, port_code=PORT["code"], event_id=event_id
        )
        assert again["evidence_count"] == 4
        assert again["event"]["point_count"] == 4
    finally:
        checker.close()


def test_unknown_event_id_returns_explicit_error(registered: TestClient) -> None:
    seed_single_event(registered)
    response = evidence(registered, 999_999)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "event_not_found"
    assert "999999" in error["message"]
    assert error["details"]["event_id"] == 999_999
    assert error["details"]["port"] == PORT["code"]


def test_event_id_from_another_port_is_not_found(tmp_path: Path) -> None:
    path = str(tmp_path / "ports.sqlite3")
    app = create_app(Settings(db_path=path))
    with TestClient(app) as local:
        local.post("/ports", json=PORT)
        other = {**PORT, "code": "CNSHA-OTHER", "min_lat": 30.0, "max_lat": 30.8}
        local.post("/ports", json=other)
        upload(
            local,
            [("SHIP-A", utc(0, m), *INSIDE) for m in (0, 10, 20, 30)],
            key="a",
        )
        event_id = local.get(
            f"/ports/{PORT['code']}/events"
        ).json()["events"][0]["event_id"]

        response = evidence(local, event_id, port="CNSHA-OTHER")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "event_not_found"
        # Still resolvable under the port that actually owns it.
        assert evidence(local, event_id, port=PORT["code"]).status_code == 200


def test_evidence_under_unknown_port_is_404(client: TestClient) -> None:
    response = client.get("/ports/NOPE/events/1/evidence")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "port_not_found"


def test_evidence_queries_inside_one_read_snapshot(registered: TestClient) -> None:
    # Sanity: header and rows agree within a single response even when paged.
    event = seed_single_event(registered)
    first = evidence(registered, event["event_id"], limit=2).json()
    assert first["event"]["point_count"] == first["evidence_count"]
    last = evidence(
        registered, event["event_id"], after=first["next_cursor"], limit=2
    ).json()
    assert last["event"]["event_id"] == event["event_id"]
    assert last["evidence_count"] == 4


def test_demo_response_includes_old_and_new_evidence_snapshots(
    client: TestClient,
) -> None:
    body = client.post("/demo/run").json()
    evidence_summary = body["evidence"]
    roles = {item["role"]: item for item in evidence_summary}
    assert set(roles) == {"retracted_old_event", "replacement_new_event"}

    old = roles["retracted_old_event"]
    new = roles["replacement_new_event"]
    assert old["status"] == "retracted"
    assert new["status"] == "active"
    # Baseline event A held 7 points; the late batch adds 2.
    assert old["evidence_count"] == old["point_count"] == 7
    assert new["evidence_count"] == new["point_count"] == 9
    assert len(old["evidence_point_ids"]) == 7
    assert len(new["evidence_point_ids"]) == 9
    # Immutable raw points are shared: the old snapshot is a strict subset of
    # the replacement's; the late points appear only in the new snapshot.
    assert set(old["evidence_point_ids"]) < set(new["evidence_point_ids"])

    # The advertised query path resolves for the retracted id as well.
    response = client.get(old["query_path"])
    assert response.status_code == 200
    assert response.json()["event"]["status"] == "retracted"


def _downgrade_to_v1(path: str) -> None:
    """Remove every v2 artifact so the database looks like a real v1 install."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE IF EXISTS event_evidence")
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT 1 FROM event_evidence")
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()[0] == "1"
    finally:
        conn.close()


def test_legacy_database_backfills_evidence_for_live_events(
    tmp_path: Path,
) -> None:
    """A v1 database (no event_evidence table) is upgraded on startup."""
    path = str(tmp_path / "legacy.sqlite3")
    db.initialise(path)

    conn = db.connect(path)
    try:
        service.register_port(
            conn,
            code=PORT["code"],
            name=PORT["name"],
            min_lat_e6=31_000_000,
            max_lat_e6=31_400_000,
            min_lon_e6=121_200_000,
            max_lon_e6=121_800_000,
        )
        rows = [
            csv_ingest.RawPointRow(
                i, "SHIP-A", 1_800_000_000 + i * 600, 31_200_000, 121_500_000
            )
            for i in range(4)
        ]
        service.ingest(
            conn,
            port_code=PORT["code"],
            upload_key="base",
            content=b"base",
            rows=rows,
        )
        # A late upload retracts the first id and publishes a replacement, so
        # the upgrade also has to handle an id retracted before the upgrade.
        late = csv_ingest.RawPointRow(
            1, "SHIP-A", 1_800_000_000 - 600, 31_200_000, 121_500_000
        )
        service.ingest(
            conn,
            port_code=PORT["code"],
            upload_key="late",
            content=b"late",
            rows=[late],
        )
        active = service.list_events(conn, port_code=PORT["code"])[0]
        new_id = active[0]["event_id"]
        retracted_id = conn.execute(
            "SELECT event_id FROM event_changes "
            "WHERE change_type = 'retract' ORDER BY seq"
        ).fetchone()[0]
    finally:
        conn.close()

    _downgrade_to_v1(path)

    # Reopening the app runs the v2 schema creation plus the backfill.
    with TestClient(create_app(Settings(db_path=path))) as local:
        response = local.get(
            f"/ports/{PORT['code']}/events/{new_id}/evidence"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["event"]["status"] == "active"
        assert body["evidence_count"] == 5
        assert body["event"]["point_count"] == 5
        assert len(body["points"]) == 5

        # The pre-upgrade retracted id remains queryable from the change log;
        # its point rows cannot be reconstructed after the fact.
        old = local.get(
            f"/ports/{PORT['code']}/events/{retracted_id}/evidence"
        ).json()
        assert old["event"]["status"] == "retracted"
        assert old["evidence_count"] == 0
        assert old["points"] == []

    # The backfill is idempotent: a second startup neither duplicates rows nor
    # fails on the now-populated snapshot table.
    with TestClient(create_app(Settings(db_path=path))) as local:
        body = local.get(
            f"/ports/{PORT['code']}/events/{new_id}/evidence"
        ).json()
        assert body["evidence_count"] == 5
        direct = sqlite3.connect(path)
        try:
            assert direct.execute(
                "SELECT COUNT(*) FROM event_evidence"
            ).fetchone()[0] == 5
            assert direct.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()[0] == "2"
        finally:
            direct.close()
