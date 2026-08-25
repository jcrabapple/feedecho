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
  version = "1.13.5";

  src = if src != null then src else fetchFromGitHub {
    owner = "jcrabapple";
    repo = "feedecho";
    rev = "v1.13.5";
    hash = lib.fakeHash; # replace after first build: `nix-prefetch-url --unpack <url>`
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
    # The wheel ships no console script; the module execs uvicorn from the
    # passthru.env runtime environment instead of the package's own bin.
    mainProgram = "uvicorn";
    platforms = platforms.linux ++ platforms.darwin;
  };
}