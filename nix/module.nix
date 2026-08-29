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

  # extraSettings values as FeedEcho reads them: strings pass through,
  # true becomes "1" (the app's on-value for every boolean setting),
  # integers are stringified. false means "leave it unset", so those keys
  # are filtered out entirely — the variable is really absent from the
  # service environment rather than set to an empty string.
  coerceEnvValue = v:
    if builtins.isBool v then (if v then "1" else "")
    else if builtins.isInt v then toString v
    else v;
  extraEnv = lib.mapAttrs (_: coerceEnvValue)
    (lib.filterAttrs (_: v: v != false) cfg.extraSettings);

  # Environment variables a dedicated option already manages. A collision in
  # extraSettings is a configuration mistake (the dedicated option always
  # wins — and FEEDECHO_AUTH_TOKEN is exported from the auth token file in
  # the service script, so extraSettings can never override it anyway).
  managedEnvKeys = [
    "FEEDECHO_DB_PATH"
    "FEEDECHO_CALLBACK_URL"
    "FEEDECHO_APP_WEBSITE"
    "FEEDECHO_AUTH_TOKEN"
  ];
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

    appWebsite = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "https://feedecho.example.com";
      description = ''
        Website registered with the Mastodon OAuth app. Mastodon links the
        "FeedEcho" application name on every post to this URL. Empty falls
        back to the project repository.
      '';
    };

    extraSettings = lib.mkOption {
      type = with lib.types; attrsOf (oneOf [ str int bool ]);
      default = { };
      example = {
        FEEDECHO_ALLOW_BACKDATED_ENTRIES = true;
        FEEDECHO_MAX_BACKDATED_ENTRY_DAYS = 7;
        FEEDECHO_LOG_LEVEL = "DEBUG";
      };
      description = ''
        Extra FeedEcho settings passed through as environment variables, for
        every setting without a dedicated option (see the "Environment
        variables" table in the repository README). Keys are used verbatim as
        variable names — use the canonical `FEEDECHO_` prefix (the legacy
        `FEEDCHO_` spelling still works but is deprecated). Values are coerced
        the way FeedEcho reads them: booleans become `1` when true and leave
        the variable unset when false, integers are stringified. A key that a
        dedicated option also manages (`dataDir`, `callbackUrl`, `appWebsite`,
        `authTokenFile`) is dropped — the dedicated option wins and a warning
        is printed at evaluation time.
      '';
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

      environment =
        let
          colliding = builtins.filter (n: builtins.hasAttr n extraEnv) managedEnvKeys;
        in
        lib.warnIf
          (colliding != [ ])
          ("services.feedecho.extraSettings sets environment variable(s) that a "
            + "dedicated option manages — ignoring: "
            + lib.concatStringsSep ", " colliding)
          ((builtins.removeAttrs extraEnv managedEnvKeys) // {
            # Managed keys are stripped from extraSettings above, so a
            # dedicated option always wins when both set the same variable.
            FEEDECHO_DB_PATH = "${cfg.dataDir}/feedecho.db";
            FEEDECHO_CALLBACK_URL = cfg.callbackUrl;
          } // lib.optionalAttrs (cfg.appWebsite != "") {
            FEEDECHO_APP_WEBSITE = cfg.appWebsite;
          });

      serviceConfig = {
        User = "feedecho";
        Group = "feedecho";
        # StateDirectory only ever creates a path under /var/lib, but dataDir is
        # configurable and ProtectSystem = "strict" makes everything else
        # read-only, so a non-default dataDir produced a service that could not
        # write its own database. Grant it explicitly.
        StateDirectory = "feedecho";
        ReadWritePaths = [ cfg.dataDir ];
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
        export FEEDECHO_AUTH_TOKEN=$(cat "$CREDENTIALS_DIRECTORY/auth_token")
        export PYTHONPATH="${pythonSitePackages}:$PYTHONPATH"
        exec ${uvicornBin} app:app --host 0.0.0.0 --port ${toString cfg.port}
      '';
    };
  };
}
