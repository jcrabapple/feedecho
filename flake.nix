{
  description = "FeedEcho — self-hosted RSS feed cross-poster";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = nixpkgs.lib.systems.flakeExposed;

      forAllSystems = f: nixpkgs.lib.genAttrs supportedSystems f;

      mkPackage = pkgs:
        let
          python = pkgs.python312;
        in
        python.pkgs.buildPythonApplication {
          pname = "feedecho";
          version = "1.30.0";
          src = ./.;
          format = "pyproject";

          nativeBuildInputs = [ python.pkgs.hatchling ];

          propagatedBuildInputs = with python.pkgs; [
            fastapi
            uvicorn
            jinja2
            python-multipart
            feedparser
            httpx
            apscheduler
          ];

          nativeCheckInputs = with python.pkgs; [
            pytest
            pytest-asyncio
          ];

          checkPhase = ''
            runHook preCheck
            python -m pytest tests/ -q
            runHook postCheck
          '';
          doCheck = false;

          # Expose the Python interpreter so the NixOS module can derive
          # the correct site-packages path, plus a consolidated runtime
          # env (uvicorn + all deps) so the module can exec uvicorn.
          passthru = {
            inherit python;
            env = python.withPackages (ps: with ps; [
              fastapi
              uvicorn
              jinja2
              python-multipart
              feedparser
              httpx
              apscheduler
            ]);
          };

          meta = with pkgs.lib; {
            description = "Self-hosted RSS feed cross-poster — route feed items to Mastodon, Bluesky, or email";
            homepage = "https://github.com/jcrabapple/feedecho";
            license = licenses.mit;
            mainProgram = "uvicorn";
            platforms = platforms.linux ++ platforms.darwin;
          };
        };

      # Build a NixOS module that receives the flake-built package so the
      # module and the package are never disconnected.
      mkNixosModule = pkgs:
        { config, lib, pkgs', ... }:
        import ./nix/module.nix {
          inherit config lib;
          pkgs = pkgs // { feedecho-flake-pkg = mkPackage pkgs; };
        };
    in
    {
      packages = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
            pkg = mkPackage pkgs;
        in {
          default = pkg;
          feedecho = pkg;
        });

      # Run the test suite via `nix build .#checks.<system>.tests`
      checks = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          tests = (mkPackage pkgs).overrideAttrs (_: { doCheck = true; });
        });

      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = with pkgs.python312.pkgs; [
              fastapi
              uvicorn
              jinja2
              python-multipart
              feedparser
              httpx
              apscheduler
              pytest
              pytest-asyncio
            ];
          };
        });

      # NixOS module — system-independent. When consumed via the flake,
      # services.feedecho.package defaults to the flake-built package.
      nixosModules.default = { config, lib, pkgs, ... }@args:
        import ./nix/module.nix {
          inherit config lib;
          pkgs = pkgs // {
            feedecho-flake-pkg =
              self.packages.${pkgs.system}.default or
              (pkgs.callPackage ./nix/package.nix { });
          };
        };
      nixosModules.feedecho = self.nixosModules.default;
    };
}
