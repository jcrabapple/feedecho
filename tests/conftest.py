"""Shared test fixtures (from the 2026-09-08 duplication audit, Tier 3 Phase A).

`db_tmp` is the 17-copy temp-database fixture collapsed into one. It is
the superset variant: files that never touched scheduler (the old "plain"
copies) simply get an inert extra monkeypatch. `test_reader.py` keeps its
own fixture (TemporaryDirectory-based, yields nothing) — it predates the
scheduler path and has no need for it.

`setup_echo` is the one canonical copy of the helper that lived, with
conflicting defaults, in both test_alt_text.py (attach_image=1) and
test_cw_and_images.py (attach_image=0). The consolidated version defaults
attach_image to 0 (the safer of the two originals: a test that wants
images must say so). Every former call site in both files was audited:
test_cw_and_images.py always passed it explicitly (default was dead code),
and test_alt_text.py's four call sites that relied on the 1 default now
pass attach_image=1 explicitly.
"""

import os
import tempfile

import pytest

import database
import scheduler


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()
    monkeypatch.setattr(scheduler, "get_db", database.get_db)

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture()
def setup_echo(db_tmp):
    """Build one mastodon account + feed + echo for dispatch tests.

    Identical to the helper that lived in test_alt_text.py and
    test_cw_and_images.py, except attach_image has no default: the two
    originals disagreed (1 vs 0), so every call site must pass it
    explicitly as a keyword argument.
    """

    def _setup(echo_overrides=None, *, attach_image=0):
        echo_kwargs = {
            "destination_type": "mastodon",
            "destination_id": 1,
            "template": "{{ title }}",
            "visibility": "public",
            "filter_keywords": "",
            "filter_mode": "exclude",
            "content_warning": "",
            "attach_image": attach_image,
            "enabled": 1,
        }
        if echo_overrides:
            echo_kwargs.update(echo_overrides)

        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token) "
                "VALUES (?, ?, ?, ?)",
                ("main", "user", "https://mastodon.social", "tok"),
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)",
                ("f", "https://example.com/feed"),
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode,
                                       content_warning, attach_image, enabled)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    echo_kwargs["destination_type"],
                    echo_kwargs["destination_id"],
                    echo_kwargs["template"],
                    echo_kwargs["visibility"],
                    echo_kwargs["filter_keywords"],
                    echo_kwargs["filter_mode"],
                    echo_kwargs["content_warning"],
                    echo_kwargs["attach_image"],
                    echo_kwargs["enabled"],
                ),
            )
            return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

    return _setup