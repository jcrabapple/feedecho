# Standalone package derivation (for non-flake NixOS users)
# Used by nix/module.nix default when not consuming via flake.nix.
{ lib
, python3
, pythonPkg ? python3
, fetchFromGitHub ? null
, src ? null
}:

pythonPkg.pkgs.buildPythonApplication {
  pname = "feedecho";
  version = "1.40.0";

  src = if src != null then src else fetchFromGitHub {
    owner = "jcrabapple";
    repo = "feedecho";
    rev = "v1.40.0";
    # Placeholder on purpose: the correct value is per-tag and can only be
    # produced by a machine with Nix. Non-flake users must set it (the build
    # prints the expected hash on first failure):
    #   nix-prefetch-url --unpack \
    #     https://github.com/jcrabapple/feedecho/archive/refs/tags/v1.40.0.tar.gz
    # Flake users never hit this path: flake.nix passes `src = self`.
    hash = lib.fakeHash;
  };

  format = "pyproject";

  nativeBuildInputs = [ pythonPkg.pkgs.hatchling ];

  propagatedBuildInputs = with pythonPkg.pkgs; [
    fastapi
    uvicorn
    jinja2
    python-multipart
    feedparser
    httpx
    apscheduler
    cryptography
  ];

  nativeCheckInputs = with pythonPkg.pkgs; [ pytest pytest-asyncio ];

  checkPhase = ''
    runHook preCheck
    python -m pytest tests/ -q
    runHook postCheck
  '';

  doCheck = false;

  # Expose the Python interpreter so the module can derive the correct
  # site-packages path without hardcoding "python3.12". Also expose a
  # consolidated runtime environment (uvicorn + all deps) so the module
  # can exec uvicorn from a single path.
  passthru = {
    python = pythonPkg;
    env = pythonPkg.withPackages (ps: with ps; [
      fastapi
      uvicorn
      jinja2
      python-multipart
      feedparser
      httpx
      apscheduler
    ]);
  };

  meta = with lib; {
    description = "Self-hosted RSS feed cross-poster — route feed items to Mastodon, Bluesky, or email";
    homepage = "https://github.com/jcrabapple/feedecho";
    license = licenses.mit;
    # No mainProgram: the wheel ships no console script (the module execs
    # uvicorn from the passthru.env runtime environment), so declaring one made
    # `nix run` fail with "program 'uvicorn' not found".
    platforms = platforms.linux ++ platforms.darwin;
  };
}