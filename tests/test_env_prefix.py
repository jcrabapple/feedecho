"""The FEEDECHO_ env prefix and its deprecated FEEDCHO_ spelling (issue #15).

The prefix shipped misspelled (``FEEDCHO_``, one E) through v1.31.0. Renaming
it outright would silently un-configure every existing deployment on upgrade —
an unauthenticated UI, a database written to a new path — so ``settings.env()``
still reads the legacy name and startup warns about each one it used.
"""

import importlib
import logging
import os

import pytest

import settings


@pytest.fixture
def clean_env(monkeypatch):
    """No FeedEcho variable of either spelling leaks in from the shell."""
    for key in list(os.environ):
        if key.startswith(("FEEDECHO_", "FEEDCHO_")):
            monkeypatch.delenv(key)
    yield monkeypatch


@pytest.fixture(autouse=True)
def restore_settings():
    """Reload settings after each test so later modules see the real env.

    Teardown in a fixture, not inline: an inline reload runs while monkeypatch
    still has the environment cleared, which leaves the module in that cleared
    state for every test that follows.
    """
    yield
    importlib.reload(settings)


class TestEnvHelper:
    def test_canonical_name_read(self, clean_env):
        clean_env.setenv("FEEDECHO_AUTH_TOKEN", "canonical")
        s = importlib.reload(settings)
        assert s.AUTH_TOKEN == "canonical"
        assert s.LEGACY_ENV_IN_USE == []

    def test_legacy_name_still_honoured(self, clean_env):
        clean_env.setenv("FEEDCHO_AUTH_TOKEN", "legacy")
        s = importlib.reload(settings)
        assert s.AUTH_TOKEN == "legacy"
        assert ("FEEDCHO_AUTH_TOKEN", "FEEDECHO_AUTH_TOKEN") in s.LEGACY_ENV_IN_USE

    def test_canonical_wins_when_both_set(self, clean_env):
        clean_env.setenv("FEEDECHO_AUTH_TOKEN", "canonical")
        clean_env.setenv("FEEDCHO_AUTH_TOKEN", "legacy")
        s = importlib.reload(settings)
        assert s.AUTH_TOKEN == "canonical"
        # The legacy value was never consulted, so nothing to deprecate.
        assert s.LEGACY_ENV_IN_USE == []

    def test_default_returned_unchanged(self, clean_env):
        """Non-str defaults survive: DB_PATH's default is a Path, not a string."""
        s = importlib.reload(settings)
        assert s.AUTH_TOKEN is None
        assert s.DB_PATH.name == "feedecho.db"
        assert s.env("NOT_SET_ANYWHERE", "fallback") == "fallback"

    def test_legacy_names_across_settings(self, clean_env):
        """Every setting, not just the token, honours the old spelling."""
        clean_env.setenv("FEEDCHO_MODE", "multi")
        clean_env.setenv("FEEDCHO_DB_PATH", "/tmp/legacy-feedecho.db")
        clean_env.setenv("FEEDCHO_BASE_URL", "https://legacy.example")
        clean_env.setenv("FEEDCHO_ALLOW_BACKDATED_ENTRIES", "1")
        clean_env.setenv("FEEDCHO_MAX_BACKDATED_ENTRY_DAYS", "9")
        s = importlib.reload(settings)
        assert s.MULTI is True
        assert str(s.DB_PATH) == "/tmp/legacy-feedecho.db"
        assert s.BASE_URL == "https://legacy.example"
        assert s.CALLBACK_URL == "https://legacy.example/oauth/callback"
        assert s.ALLOW_BACKDATED_ENTRIES is True
        assert s.MAX_BACKDATED_ENTRY_DAYS == 9

    def test_legacy_list_does_not_duplicate(self, clean_env):
        clean_env.setenv("FEEDCHO_AUTH_TOKEN", "legacy")
        s = importlib.reload(settings)
        s.env("AUTH_TOKEN")
        s.env("AUTH_TOKEN")
        assert s.LEGACY_ENV_IN_USE.count(
            ("FEEDCHO_AUTH_TOKEN", "FEEDECHO_AUTH_TOKEN")
        ) == 1


class TestDeprecationWarning:
    def test_warns_naming_each_legacy_variable(self, clean_env, caplog):
        clean_env.setenv("FEEDCHO_AUTH_TOKEN", "legacy")
        clean_env.setenv("FEEDCHO_BASE_URL", "https://legacy.example")
        s = importlib.reload(settings)
        with caplog.at_level(logging.WARNING, logger="feedecho"):
            s.warn_legacy_env()
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "FEEDCHO_AUTH_TOKEN -> FEEDECHO_AUTH_TOKEN" in message
        assert "FEEDCHO_BASE_URL -> FEEDECHO_BASE_URL" in message

    def test_silent_when_no_legacy_names_used(self, clean_env, caplog):
        clean_env.setenv("FEEDECHO_AUTH_TOKEN", "canonical")
        s = importlib.reload(settings)
        with caplog.at_level(logging.WARNING, logger="feedecho"):
            s.warn_legacy_env()
        assert caplog.records == []

    def test_validate_config_warns_in_single_mode(self, clean_env, caplog):
        """Single mode returns early from every other check but still warns.

        A self-hoster on the old names is exactly who needs to be told, and
        validate_config() is the first hook that runs after logging is set up.
        """
        clean_env.setenv("FEEDCHO_AUTH_TOKEN", "legacy")
        s = importlib.reload(settings)
        assert s.MULTI is False
        with caplog.at_level(logging.WARNING, logger="feedecho"):
            s.validate_config()  # must not raise
        assert any(
            "FEEDCHO_AUTH_TOKEN" in r.getMessage() for r in caplog.records
        )


class TestLoggingLevelPrefix:
    @pytest.fixture(autouse=True)
    def restore_logging_state(self):
        import logging_setup

        root = logging.getLogger()
        saved = (logging_setup._configured, root.level, list(root.handlers))
        yield
        logging_setup._configured, level, handlers = saved
        root.setLevel(level)
        root.handlers = handlers

    def test_legacy_log_level_honoured(self, clean_env):
        import logging_setup

        clean_env.setenv("FEEDCHO_LOG_LEVEL", "DEBUG")
        logging_setup._configured = False
        logging_setup.setup_logging()
        assert logging.getLogger().level == logging.DEBUG

    def test_canonical_log_level_wins(self, clean_env):
        import logging_setup

        clean_env.setenv("FEEDECHO_LOG_LEVEL", "ERROR")
        clean_env.setenv("FEEDCHO_LOG_LEVEL", "DEBUG")
        logging_setup._configured = False
        logging_setup.setup_logging()
        assert logging.getLogger().level == logging.ERROR


class TestNoStrayLegacySpelling:
    def test_repo_reads_only_the_canonical_prefix(self):
        """No file may read a FEEDCHO_ variable directly again.

        The legacy spelling is allowed in exactly two places: settings.py
        (the compatibility shim) and the tests that exercise it. Anywhere
        else it is the misspelling from issue #15 creeping back in.
        """
        import subprocess
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        allowed = {
            "settings.py",
            "logging_setup.py",
            "tests/test_env_prefix.py",
            "tests/test_settings.py",
            "README.md",
            # Docs that tell Nix users the deprecated spelling exists (the
            # module passes extraSettings keys through verbatim, so its docs
            # must name the legacy prefix). Neither file reads it — this
            # exemption covers documentation strings only; if module.nix ever
            # starts reading FEEDCHO_ directly, this entry must be revisited.
            "nix/README.md",
            "nix/module.nix",
            # Reads the VPS credential env vars, whose Infisical keys were
            # deliberately never renamed (FEEDCHO_VPS_IP / FEEDCHO_VPS_USER /
            # FEEDCHO_VPS_PASSWORD) — see the note in the feedecho skill and
            # the env-prefix rename issue #15. The script is shell, not
            # Python, so it cannot go through settings.env()'s shim; it reads
            # those exact keys directly.
            "scripts/backup-verify-pull.sh",
        }
        offenders = []
        for rel in tracked:
            if rel in allowed:
                continue
            path = repo / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            # "FEEDCHO_" without the second E, and not part of "FEEDECHO_".
            if "FEEDCHO_" in text:
                offenders.append(rel)
        assert offenders == []
