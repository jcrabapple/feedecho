# NixOS

FeedEcho ships a Nix flake and a NixOS module for declarative deployments.

## Flake (recommended)

```nix
# flake.nix in your NixOS config
{
  inputs.feedecho.url = "github:jcrabapple/feedecho";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs, feedecho, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        feedecho.nixosModules.default
        {
          services.feedecho = {
            enable = true;
            authTokenFile = "/run/secrets/feedecho-token";
            callbackUrl = "https://feedecho.example.com/oauth/callback";
            openFirewall = true;
          };
        }
      ];
    };
  };
}
```

### Dev shell

```bash
nix develop github:jcrabapple/feedecho
# or from a checkout:
nix develop
```

### Build / test

```bash
nix build                    # build the package
nix build .#checks.x86_64-linux.tests  # run the test suite (nix build .#checks.<system>.tests)
```

## Non-flake NixOS (legacy channel)

Add to your `configuration.nix`:

```nix
{ pkgs, lib, config, ... }:

let
  feedechoSrc = builtins.fetchTree {
    type = "github";
    owner = "jcrabapple";
    repo = "feedecho";
    rev = "v1.13.2";
  };
  feedechoPkg = pkgs.callPackage (feedechoSrc + "/nix/package.nix") { src = feedechoSrc; };
in {
  # Import the module and pass the package
  imports = [ (feedechoSrc + "/nix/module.nix") ];

  # Override the module's default package with the one we built
  services.feedecho.package = feedechoPkg;

  services.feedecho = {
    enable = true;
    authTokenFile = "/run/secrets/feedecho-token";
    callbackUrl = "https://feedecho.example.com/oauth/callback";
  };
}
```

**Note:** Passing `src = feedechoSrc` builds from your fetched checkout, so no
dependency hash is needed. If you omit `src`, `nix/package.nix` falls back to
fetching from GitHub and needs its `fetchFromGitHub` hash set — it is
`lib.fakeHash` by default. Replace it after the first build:

```bash
nix-prefetch-url --unpack https://github.com/jcrabapple/feedecho/archive/refs/tags/v1.13.2.tar.gz
```

## Auth token

Write your token to a file readable by the `feedecho` user:

```bash
echo -n "your-long-random-token" > /run/secrets/feedecho-token
chmod 400 /run/secrets/feedecho-token
chown feedecho:feedecho /run/secrets/feedecho-token
```

Use `sops-nix` or `agenix` to manage the secret file declaratively.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `services.feedecho.enable` | `false` | Enable the service |
| `services.feedecho.port` | `8453` | Web UI port |
| `services.feedecho.package` | flake-built or `callPackage` | FeedEcho package |
| `services.feedecho.authTokenFile` | (required) | Path to file containing auth token |
| `services.feedecho.callbackUrl` | `http://localhost:8453/oauth/callback` | Public OAuth callback URL |
| `services.feedecho.dataDir` | `/var/lib/feedecho` | SQLite database directory |
| `services.feedecho.openFirewall` | `false` | Open firewall for the port |

## Notes

- The app runs as a dedicated `feedecho` system user
- SQLite lives in `dataDir` (default `/var/lib/feedecho`)
- `StateDirectory=feedecho` ensures systemd manages `/var/lib/feedecho`; keep `dataDir` at the default or override both consistently
- Put nginx or Caddy in front for TLS — point the reverse proxy at port `8453`
- The flake uses `nixos-unstable`; pin to a specific nixpkgs revision if you need reproducibility
- The module accepts the flake-built package via `pkgs.feedecho-flake-pkg`; non-flake users override `services.feedecho.package` explicitly
