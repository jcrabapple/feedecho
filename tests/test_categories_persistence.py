"""{{ categories }} — RSS/Atom category domains persisted through the DB round-trip.

The parser extracts them (PR #45); these tests pin the persistence half:
_store_feed_items stores the dict as JSON, and the DB-rebuilt item dicts
(compose desk, post-now, queue re-render) get it back via
categories_from_value so templates render categories everywhere, not just
on the fresh-parse automatic echo path.
"""

import json
import os
import tempfile

import pytest


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    import scheduler
    import notify

    monkeypatch.setattr(scheduler, "get_db", database.get_db)
    monkeypatch.setattr(notify, "get_db", database.get_db)

    yield database, scheduler

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _mk_item(id, categories=None):
    item = {"id": id, "title": f"t-{id}", "link": f"https://example.com/{id}"}
    if categories is not None:
        item["categories"] = categories
    return item


class TestCategoriesPersistence:
    def test_store_round_trips_categories(self, env):
        database, scheduler = env
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))
        cats = {
            "http://albopop.it/specs#item-category-type": "MUNICIPIO 7",
            "item-category-type": "MUNICIPIO 7",
            "item-category-uid": "DGM7 32/2026",
        }
        scheduler._store_feed_items(1, [_mk_item("a", cats)])
        with database.get_db() as db:
            row = db.execute(
                "SELECT categories FROM feed_items WHERE item_id = 'a'"
            ).fetchone()
        assert json.loads(row["categories"]) == cats

    def test_store_defaults_to_empty_dict(self, env):
        database, scheduler = env
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))
        scheduler._store_feed_items(1, [_mk_item("a")])
        with database.get_db() as db:
            row = db.execute(
                "SELECT categories FROM feed_items WHERE item_id = 'a'"
            ).fetchone()
        assert json.loads(row["categories"]) == {}

    def test_update_overwrites_categories(self, env):
        database, scheduler = env
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))
        scheduler._store_feed_items(1, [_mk_item("a", {"d#k": "old"})])
        scheduler._store_feed_items(1, [_mk_item("a", {"d#k": "new"})])
        with database.get_db() as db:
            row = db.execute(
                "SELECT categories FROM feed_items WHERE item_id = 'a'"
            ).fetchone()
        assert json.loads(row["categories"]) == {"d#k": "new"}


class TestCategoriesFromValue:
    def test_none_and_empty_read_as_empty(self):
        from utils import categories_from_value

        assert categories_from_value(None) == {}
        assert categories_from_value("") == {}

    def test_malformed_json_reads_as_empty(self):
        from utils import categories_from_value

        assert categories_from_value("{not json") == {}

    def test_non_dict_payload_reads_as_empty(self):
        from utils import categories_from_value

        assert categories_from_value('["a", "b"]') == {}

    def test_valid_json_round_trips(self):
        from utils import categories_from_value

        assert categories_from_value('{"d#k": "v"}') == {"d#k": "v"}


class TestCategoriesInRebuiltItemDicts:
    """The DB-rebuild sites must hand {{ categories }} to the template engine."""

    def _seed(self, database, categories_json):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, link, categories)"
                " VALUES (1, 'a', 't-a', 'https://example.com/a', ?)",
                (categories_json,),
            )
            row = db.execute(
                "SELECT i.*, f.name AS feed_name FROM feed_items i"
                " JOIN feeds f ON i.feed_id = f.id WHERE i.item_id = 'a'"
            ).fetchone()
        return row

    def test_row_value_parses_to_dict(self, env):
        database, _scheduler = env
        row = self._seed(
            database,
            json.dumps({
                "http://albopop.it/specs#item-category-type": "MUNICIPIO",
                "item-category-type": "MUNICIPIO",
            }),
        )
        from utils import categories_from_value

        cats = categories_from_value(row["categories"])
        assert cats["http://albopop.it/specs#item-category-type"] == "MUNICIPIO"
        # And it renders through the same context the rebuild sites feed.
        from template_engine import render_template

        rendered = render_template(
            "{{ categories['item-category-type'] }}", {"categories": cats}
        )
        assert rendered == "MUNICIPIO"

    def test_null_column_yields_empty_dict(self, env):
        database, _scheduler = env
        row = self._seed(database, None)
        from utils import categories_from_value

        assert categories_from_value(row["categories"]) == {}
