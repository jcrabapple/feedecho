"""Tests for reader folders (Phase 1.1)."""

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app


@pytest.fixture
def folder_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reader-folders.db")
    database.init_db()
    import scheduler

    monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": []})
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled, user_id) VALUES (?, ?, 1, 1)",
            ("Tech News", "https://example.com/tech"),
        )
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled, user_id) VALUES (?, ?, 1, 1)",
            ("Science", "https://example.com/science"),
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, published_at, is_read)"
            " VALUES (1, 't1', 'Tech Post 1', '2026-01-01 10:00:00', 0)"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, published_at, is_read)"
            " VALUES (2, 's1', 'Science Post 1', '2026-01-01 11:00:00', 0)"
        )
    return settings


def test_folder_crud(folder_env):
    client = TestClient(app)

    # Create folder
    r = client.post("/api/folders", data={"name": "News"}, follow_redirects=False)
    assert r.status_code == 303
    with database.get_db() as db:
        fol = db.execute("SELECT * FROM folders WHERE name = 'News'").fetchone()
    assert fol is not None
    folder_id = fol["id"]

    # Duplicate name -> 409
    r_dup = client.post("/api/folders", data={"name": "news"})
    assert r_dup.status_code == 409

    # Rename
    r_ren = client.post(f"/api/folders/{folder_id}/rename", data={"name": "Tech & News"}, follow_redirects=False)
    assert r_ren.status_code == 303
    with database.get_db() as db:
        fol = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    assert fol["name"] == "Tech & News"

    # Set feed folder
    r_assign = client.post("/api/feeds/1/folder", data={"folder_id": str(folder_id)})
    assert r_assign.status_code == 200
    with database.get_db() as db:
        f = db.execute("SELECT folder_id FROM feeds WHERE id = 1").fetchone()
    assert f["folder_id"] == folder_id

    # Unassign feed folder
    r_unassign = client.post("/api/feeds/1/folder", data={"folder_id": ""})
    assert r_unassign.status_code == 200
    with database.get_db() as db:
        f = db.execute("SELECT folder_id FROM feeds WHERE id = 1").fetchone()
    assert f["folder_id"] is None

    # Re-assign and delete folder -> feed folder_id becomes NULL, feeds survive
    client.post("/api/feeds/1/folder", data={"folder_id": str(folder_id)})
    r_del = client.post(f"/api/folders/{folder_id}/delete", follow_redirects=False)
    assert r_del.status_code == 303
    with database.get_db() as db:
        fol = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        f = db.execute("SELECT folder_id FROM feeds WHERE id = 1").fetchone()
    assert fol is None
    assert f["folder_id"] is None


def test_folder_authz_isolation(folder_env):
    """Users cannot edit, delete, or assign folders belonging to another user."""
    with database.get_db() as db:
        # Create folder for user 2
        db.execute("INSERT INTO folders (user_id, name, position) VALUES (2, 'Other User Folder', 1)")
        other_fol_id = db.execute("SELECT id FROM folders WHERE user_id = 2").fetchone()["id"]

    client = TestClient(app)  # executes as user 1

    assert client.post(f"/api/folders/{other_fol_id}/rename", data={"name": "Hacked"}).status_code == 404
    assert client.post(f"/api/folders/{other_fol_id}/delete").status_code == 404
    assert client.post("/api/feeds/1/folder", data={"folder_id": str(other_fol_id)}).status_code == 404


def test_reader_folder_filtering_and_query_operator(folder_env):
    with database.get_db() as db:
        db.execute("INSERT INTO folders (user_id, name, position) VALUES (1, 'Computing', 1)")
        fid = db.execute("SELECT id FROM folders WHERE name = 'Computing'").fetchone()["id"]
        db.execute("UPDATE feeds SET folder_id = ? WHERE id = 1", (fid,))

    client = TestClient(app)

    # 1. folder= param filtering
    r_folder = client.get(f"/reader?view=all&folder={fid}")
    assert r_folder.status_code == 200
    assert "Tech Post 1" in r_folder.text
    assert "Science Post 1" not in r_folder.text

    # 2. folder: operator in search query
    r_op = client.get("/reader?q=folder:comput")
    assert r_op.status_code == 200
    assert "Tech Post 1" in r_op.text
    assert "Science Post 1" not in r_op.text


def test_reader_mark_folder_read(folder_env):
    with database.get_db() as db:
        db.execute("INSERT INTO folders (user_id, name, position) VALUES (1, 'Science Group', 1)")
        fid = db.execute("SELECT id FROM folders WHERE name = 'Science Group'").fetchone()["id"]
        db.execute("UPDATE feeds SET folder_id = ? WHERE id = 2", (fid,))

    client = TestClient(app)
    r = client.post("/api/reader/mark-all-read", data={"folder_id": fid})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1

    with database.get_db() as db:
        # Feed 2 (Science) item read state updated
        sci = db.execute("SELECT is_read FROM feed_items WHERE id = 2").fetchone()
        tech = db.execute("SELECT is_read FROM feed_items WHERE id = 1").fetchone()
    assert sci["is_read"] == 1
    assert tech["is_read"] == 0
