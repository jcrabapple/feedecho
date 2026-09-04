"""Single source of truth for the running version (issue #8).

Everything Python-side reads this: the FastAPI/OpenAPI version, the version
shown in the site footer, and the packaging version (``pyproject.toml``
declares ``dynamic = ["version"]`` and points hatchling here).

The deploy artifacts that cannot import Python at evaluation time still carry
their own copy — ``flake.nix``, ``nix/package.nix``, ``nix/README.md`` and
``docker-compose.multi.yml``. ``scripts/bump_version.py`` rewrites all of them
in one step, and ``tests/test_version.py`` pins them to the value below (plus a
scan for version-shaped strings elsewhere in the tree), so a partial bump fails
the suite instead of shipping a UI that lies about which release is running.
"""

__version__ = "1.47.6"
