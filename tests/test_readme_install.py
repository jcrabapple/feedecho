"""README install and update instructions must stay true.

The Docker image installs from the hash-pinned requirements.txt, so a
dependency added to pyproject.toml but left off the README's from-source
`pip install` line breaks only from-source installs, and only at first import
(`import nh3` in feed_parser raised ModuleNotFoundError for several releases
while the README line omitted it).

The Updating section exists because a running install never updates itself:
the cost of that gap was a self-hoster still on a pre-link-card build three
weeks after the fix shipped.
"""

import re
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _dep_name(spec: str) -> str:
    """'uvicorn[standard]>=0.34.0' -> 'uvicorn' (PEP 503-ish normalization)."""
    head = re.split(r"[\s\[<>=!~;]", spec.strip().strip("\"'"), maxsplit=1)[0]
    return head.lower().replace("_", "-")


def _section(heading: str) -> str:
    """Body of the README section starting at `heading`, up to the next heading
    of the same or a higher level. Headings inside fenced code blocks do not
    count: `# comment` lines in a bash snippet look like level-1 headings."""
    level = len(heading) - len(heading.lstrip("#"))
    out: list[str] = []
    capture = fenced = False
    for line in README.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced:
            match = re.match(r"(#{1,6}) ", line)
            if match:
                if capture and len(match.group(1)) <= level:
                    break
                if line.strip() == heading:
                    capture = True
        if capture:
            out.append(line)
    assert out, f"README has no section {heading!r}"
    return "\n".join(out)


def test_from_source_pip_line_lists_every_pyproject_dependency():
    pip_lines = [
        line for line in _section("### From source").splitlines()
        if line.startswith("pip install ")
    ]
    assert len(pip_lines) == 1, "expected exactly one pip install line under 'From source'"

    listed = {_dep_name(tok) for tok in shlex.split(pip_lines[0])[2:]}
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = {_dep_name(dep) for dep in pyproject["project"]["dependencies"]}

    missing = sorted(required - listed)
    assert not missing, f"README 'From source' pip line is missing: {missing}"


class TestUpdatingSection:
    def test_section_sits_under_quick_start_before_usage(self):
        quick = README.index("## Quick Start")
        updating = README.index("### Updating")
        usage = README.index("## Usage")
        assert quick < updating < usage

    def test_names_the_update_command_for_every_install_method(self):
        body = _section("### Updating")
        # pre-built image: pull THEN up (a bare `up -d` reuses the cached image)
        assert "docker compose pull" in body
        assert "docker compose up -d" in body
        assert "--pull always" in body
        assert "pull_policy: always" in body
        # built from a clone: nothing to pull, rebuild instead
        assert "docker compose up -d --build" in body
        # plain docker, from source, nix
        assert "docker build -t feedecho ." in body
        assert "docker rm -f feedecho" in body
        assert "nix/README.md" in body

    def test_says_data_survives_and_where_to_see_the_version(self):
        body = _section("### Updating")
        assert "feedecho-data" in body
        assert "footer" in body

    def test_pull_policy_line_the_docs_point_at_exists_in_the_compose_file(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert re.search(r"^\s*#\s*pull_policy:\s*always\s*$", compose, re.M), (
            "README tells users to uncomment `pull_policy: always` in docker-compose.yml"
        )

    def test_docker_section_points_at_updating(self):
        assert "(#updating)" in _section("### Docker (recommended)")
