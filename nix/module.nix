# NixOS module for FeedEcho
#
# Add to your configuration:
#
#   services.feedecho = {
#     enable = true;
#     authTokenFile = "/run/secrets/feedecho-token";
#     callbackUrl = "https://feedecho.example.com/oauth/callback";
#   };
#
# Then `nixos-rebuild switch`. The app runs on port 8453 by default.
# Put a reverse proxy (nginx, caddy) in front for TLS.

{ config, lib, pkgs, ... }:

let
  cfg = config.services.feedecho;

  # Prefer the flake-built package injected by flake.nix; fall back to
  # callPackage for non-flake (channel) users.
  defaultPkg = pkgs.feedecho-flake-pkg or (pkgs.callPackage ./package.nix { });

  # Derive the site-packages path from the package's Python interpreter
  # instead of hardcoding "python3.12".
  pythonSitePackages =
    if cfg.package ? python then
      "${cfg.package}/lib/${cfg.package.python.libPrefix}/site-packages"
    else
      "${cfg.package}/lib/python3.12/site-packages";

  # The wheel does not ship a console script, so exec uvicorn from the
  # package's consolidated runtime env (uvicorn + all deps).
  uvicornBin =
    if cfg.package ? env then
      "${cfg.package.env}/bin/uvicorn"
    else if cfg.package ? python then
      "${cfg.package.python.pkgs.uvicorn}/bin/uvicorn"
    else
      "${pkgs.python3.pkgs.uvicorn}/bin/uvicorn";
in {
  options.services.feedecho = {
    enable = lib.mkEnableOption "FeedEcho RSS feed cross-poster";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPkg;
      defaultText = lib.literalExpression "pkgs.feedecho-flake-pkg or (pkgs.callPackage ./nix/package.nix { })";
      description = "FeedEcho package to use.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8453;
      description = "Port for the FeedEcho web UI.";
    };

    authTokenFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to a file containing the auth token for the FeedEcho web UI.
        Use `services.feedecho.authToken` for testing only —
        prefer a secret file for real deployments.
      '';
    };

    callbackUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://localhost:8453/oauth/callback";
      description = "Public callback URL for Mastodon OAuth.";
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/feedecho";
      description = "Directory for the SQLite database.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the firewall for the configured port.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.feedecho = {
      isSystemUser = true;
      group = "feedecho";
      home = cfg.dataDir;
      createHome = true;
    };
    users.groups.feedecho = { };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.feedecho = {
      description = "FeedEcho RSS feed cross-poster";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        FEEDCHO_DB_PATH = "${cfg.dataDir}/feedecho.db";
        FEEDCHO_CALLBACK_URL = cfg.callbackUrl;
      };

      serviceConfig = {
        User = "feedecho";
        Group = "feedecho";
        StateDirectory = "feedecho";
        LoadCredential = "auth_token:${cfg.authTokenFile}";
        Restart = "on-failure";
        RestartSec = 5;

        # Hardening
        NoNewPrivileges = true;

        # Process isolation
        PrivateMounts = true;
        PrivateTmp = true;
        RemoveIPC = true;

        # Filesystem
        ProtectHome = true;
        ProtectSystem = "strict";
        UMask = "0077";

        # Kernel
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";

        # Capabilities
        AmbientCapabilities = [ ];
        CapabilityBoundingSet = [ ];
        RestrictSUIDSGID = true;

        # Memory
        LockPersonality = true;
        MemoryDenyWriteExecute = true;

        # Devices
        PrivateDevices = true;

        # Network
        # HTTP(S) and Unix sockets for the web UI, outbound feed/API polling,
        # SMTP, and DNS resolution via the resolver socket.
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK" ];

        # Misc
        KeyringMode = "private";
        RestrictNamespaces = true;
        RestrictRealtime = true;
      };

      # Read the auth token from the credential file and exec uvicorn.
      # app.py, templates/, and static/ live in the package's own
      # site-packages; uvicorn and the app's dependencies live in the
      # consolidated runtime env. Put both on PYTHONPATH so the "app"
      # import resolves.
      script = ''
        export FEEDCHO_AUTH_TOKEN=$(cat "$CREDENTIALS_DIRECTORY/auth_token")
        export PYTHONPATH="${pythonSitePackages}:$PYTHONPATH"
        exec ${uvicornBin} app:app --host 0.0.0.0 --port ${toString cfg.port}
      '';
    };
  };
}
