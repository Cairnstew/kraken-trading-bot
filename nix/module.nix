# nix/module.nix — NixOS module for kraken-trading-bot
#
# Provides the package on PATH plus credential/environment configuration.
# Import from the flake:
#
#   inputs.kraken-trading-bot.url = "github:Cairnstew/kraken-trading-bot";
#
#   imports = [ inputs.kraken-trading-bot.nixosModules.default ];
#   services.kraken-trading-bot.enable = true;
#
# Credentials can be supplied three ways (first wins):
#   1. credentials.envFile     — path to an existing .env file (takes
#      precedence; no env file is generated at all).
#   2. credentials.api*File    — keyfile paths (e.g. agenix-managed
#      /run/secrets/...).  The secret is resolved by systemd at
#      activation time, so it never enters /nix/store.  The generated
#      env file defaults to /run/kraken-trading-bot/.env (mode 0600).
#   3. credentials.api*/...    — plain string values (kept for
#      convenience, but note they end up world-readable in the Nix
#      store; prefer the *File options for real secrets).
#
# The generated env file is loaded automatically by the app (python-dotenv)
# and is also suitable as a systemd EnvironmentFile:
#
#   systemd.services.foo.serviceConfig.EnvironmentFile =
#     [ config.services.kraken-trading-bot.envFilePath ];

{ config, lib, pkgs, ... }:

let
  cfg = config.services.kraken-trading-bot;

  # Single-quote a string for embedding in a sh script.
  shellQuote = v: "'" + builtins.replaceStrings [ "'" ] [ "'\\''" ] v + "'";

  # One KRAKEN_* line as literal shell that writes the value into the env
  # file.  Nix-supplied values are written literally (copying them into the
  # store is unavoidable); keyfile values are resolved by `cat` at runtime
  # so the secret never appears in the store.
  valueLine = name: value: "echo ${shellQuote "${name}='${value}'"}";
  fileLine = name: file: "printf '%s\\n' \"${name}=$(cat ${shellQuote file})\"";
in
{
  # ── Options ─────────────────────────────────────────────────────────────
  options.services.kraken-trading-bot = {
    enable = lib.mkEnableOption "kraken-trading-bot CLI";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.kraken-trading-bot;
      defaultText = "pkgs.kraken-trading-bot";
      description = "The kraken-trading-bot package to use.";
    };

    # Where the generated credential file is written.  Exposed so other
    # systemd services can reference it as an EnvironmentFile.
    envFilePath = lib.mkOption {
      type = lib.types.str;
      default = "/run/kraken-trading-bot/.env";
      description = "Path of the env file written by the credential oneshot.";
    };

    credentials = {
      # Plain-string credentials.  These are baked into the Nix store
      # (world-readable), so prefer apiKeyFile/apiSecretFile for anything
      # you would not print in public.
      apiKey = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "KRAKEN_API_KEY value (public API key).";
      };

      apiSecret = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "KRAKEN_API_SECRET value (private signing secret).";
      };

      # Keyfile credentials — e.g. agenix-managed paths such as
      # /run/secrets/kraken_api_key.  The file is read by the systemd
      # oneshot at activation time; its contents never enter /nix/store.
      apiKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          Path to a file whose first line is the API key (e.g. an
          agenix-managed /run/secrets path).  Takes precedence over
          credentials.apiKey when both are set.
        '';
      };

      apiSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          Path to a file whose first line is the API secret (e.g. an
          agenix-managed /run/secrets path).  Takes precedence over
          credentials.apiSecret when both are set.
        '';
      };

      # Path to an existing .env file (alternative to setting individual
      # credential options above).  When set, this takes precedence over
      # everything and no env file is generated.
      envFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Path to a .env file with KRAKEN_* variables.";
      };

      # systemd units the credential oneshot must wait for.  When using the
      # *File options with a secrets manager (agenix, sops-nix, ...), add
      # the unit(s) that materialize those files here so the env file is
      # not written (and fail) before the secrets exist.
      after = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "agenix-activation.service" ];
        description = ''
          Units to order the credential writer after.  Defaults to
          agenix v1's activation unit; adjust for your secrets manager
          (e.g. sops-nix.service, agenix2's units, or individual
          age-<secret>.service units).
        '';
      };
    };

    # App-level (non-secret) KRAKEN_* configuration.  Kept separate from
    # credentials so code review does not have to wonder which values are
    # sensitive.
    settings = {
      restUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "KRAKEN_REST_URL override (REST base URL).";
      };

      wsUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "KRAKEN_WS_URL override (public WebSocket v2 url).";
      };

      wsAuthUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "KRAKEN_WS_AUTH_URL override (private WebSocket v2 url).";
      };

      minInterval = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "KRAKEN_MIN_INTERVAL minimum seconds between REST calls (e.g. \"0.1\").";
      };

      paperMode = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "KRAKEN_PAPER_MODE enable paper trading (\"true\" or \"false\").";
      };

      paperBalance = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "KRAKEN_PAPER_BALANCE opening quote balance for paper trading (e.g. \"10000\").";
      };

      # Catch-all for any other KRAKEN_* variable (written verbatim).
      # Null values are skipped, which lets you explicitly clear an
      # inherited environment variable.
      extra = lib.mkOption {
        type = lib.types.attrsOf (lib.types.nullOr lib.types.str);
        default = { };
        description = "Extra KRAKEN_* environment variables (NAME = value).";
      };
    };

    # ── Recorders ─────────────────────────────────────────────────────────
    # Unattended data collection.  Separate from `settings` because these are
    # SYSTEM units that read no credential and write a file, not app
    # configuration.
    recorders = {
      orderBook = {
        enable = lib.mkEnableOption ''
          the hourly Kraken order-book DEPTH recorder (G1).  Off by default
          because it writes a file that grows forever.

          Kraken's order book is a LIVE SNAPSHOT WITH NO HISTORICAL ENDPOINT,
          so this is the only feature here whose absence destroys data
          permanently rather than merely leaving a column empty.  If you run
          the bot at all, turn this on.
        '';

        pair = lib.mkOption {
          type = lib.types.str;
          default = "ETH/USD";
          description = "Spot pair to snapshot (keyless /0/public/Depth).";
        };

        output = lib.mkOption {
          type = lib.types.str;
          default = "/var/lib/kraken-trading-bot/signals/eth_usd_orderbook.jsonl";
          defaultText = "/var/lib/kraken-trading-bot/signals/eth_usd_orderbook.jsonl";
          description = ''
            Append-only JSONL log.  Created if absent; the parent directory is
            created by `StateDirectory`, so it must live under
            /var/lib/kraken-trading-bot unless you manage it yourself.
          '';
        };

        count = lib.mkOption {
          type = lib.types.int;
          default = 100;
          description = ''
            Requested book depth per side.  100 is Kraken's served default and
            the largest depth actually honoured: MEASURED 2026-10-03,
            `count=1000` is served as 100 levels per side with no error, i.e. a
            silent depth REDUCTION.  Every record stores the requested depth
            and the depth actually returned, so a `count` change is visible in
            the artifact rather than silently making two records incomparable.
          '';
        };

        expectedIntervalSeconds = lib.mkOption {
          type = lib.types.int;
          default = 3600;
          description = ''
            The cadence the gap counter holds the recorder to.  An interval
            longer than this x `gapFactor` is reported as a hole, in the
            report and in the status sidecar.
          '';
        };

        gapFactor = lib.mkOption {
          type = lib.types.float;
          default = 1.5;
          description = ''
            Hole threshold multiplier.  1.5 tolerates the timer's
            `RandomizedDelaySec` jitter (which can legitimately stretch one
            interval to ~1h4m) while still catching a single missed fire.
          '';
        };

onCalendar = lib.mkOption {
            type = lib.types.str;
            default = "*-*-* *:41:00";
            defaultText = "*-*-* *:41:00";
            description = ''
              systemd OnCalendar for the timer.  The default is hourly at :41 —
              a minute clear of the funding (:17), news (:23) and social (:29)
              pullers.  Hourly is also the CADENCE FLOOR: the bar interval is
              `ohlcv_interval_minutes: 60`, so anything finer pays more calls to
              keep the same usable rows.
            '';
          };

          # Off-machine copy.  Off by default, and separately so from `enable`:
          # the recorder collecting locally and the archive leaving the machine
          # are different decisions, and a host may reasonably want the first
          # without volunteering its unrecoverable data to a remote.
          checkpoint = {
            enable = lib.mkEnableOption "the weekly off-machine copy of the depth log" // {
              default = false;
              description = ''
                Copy the order-book depth log off this machine weekly.  Off by
                default.  The log is unrecoverable (Kraken serves no historical
                Depth endpoint), so a lost disk is lost data — but enabling this
                sends that data to a REMOTE, which is the lead's decision, not a
                default.  §8.1 of .data-audit/DECISION.md requires asking first.
              '';
            };

            onCalendar = lib.mkOption {
              type = lib.types.str;
              default = "Mon *-*-* 04:23:00";
              defaultText = "Mon *-*-* 04:23:00";
              example = "Mon *-*-* 04:23:00";
              description = ''
                When the weekly copy runs.  Monday 04:23, deliberately not on
                the hour: every other timer in this module fires at :17/:23/:29
                or :41, so a distinct minute keeps the checkpoint's journal
                lines separable from them.  Early in the week, so a failure is
                noticed with time to fix it before the next run.
              '';
            };

            remote = lib.mkOption {
              type = lib.types.str;
              # SSH, not HTTPS: the credential is a per-repo deploy key, which an
              # HTTPS PAT cannot be.  A PAT is scoped by what the TOKEN may do; a
              # deploy key is scoped by what the KEY is registered against.
              default = "git@github.com:Cairnstew/kraken-depth-archive.git";
              example = "git@github.com:Cairnstew/kraken-depth-archive.git";
              description = ''
                Git remote for the archive.  Must be reachable WITHOUT an
                interactive prompt — the unit runs unattended, so a credential
                that needs a TTY or a passphrase fails silently every week,
                which looks exactly like a recorder that works.  The recipe
                refuses any destination on the same physical disk as the log.
              '';
            };
          };

        statusOutput = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = ''
            Path of the gap-report sidecar, rewritten on every fire.  Null
            means "<output>.status.json".  This is the file that makes a hole
            visible in the artifact and not only on stdout.
          '';
        };
      };
    };
  };

  # ── Implementation ──────────────────────────────────────────────────────
  config = lib.mkIf cfg.enable (
    let
      writeEnv =
        cfg.credentials.envFile == null
        && (cfg.credentials.apiKey != ""
        || cfg.credentials.apiSecret != ""
        || cfg.credentials.apiKeyFile != null
        || cfg.credentials.apiSecretFile != null
        || cfg.settings.restUrl != null
        || cfg.settings.wsUrl != null
        || cfg.settings.wsAuthUrl != null
        || cfg.settings.minInterval != null
        || cfg.settings.paperMode != null
        || cfg.settings.paperBalance != null
        || cfg.settings.extra != { });

      # Individual env lines, in stable order: credentials first, then
      # settings, then catch-all extras.
      envLines =
        (lib.optional (cfg.credentials.apiKeyFile != null) (fileLine "KRAKEN_API_KEY" cfg.credentials.apiKeyFile))
        ++ lib.optional (cfg.credentials.apiKeyFile == null && cfg.credentials.apiKey != "") (valueLine "KRAKEN_API_KEY" cfg.credentials.apiKey)
        ++ lib.optional (cfg.credentials.apiSecretFile != null) (fileLine "KRAKEN_API_SECRET" cfg.credentials.apiSecretFile)
        ++ lib.optional (cfg.credentials.apiSecretFile == null && cfg.credentials.apiSecret != "") (valueLine "KRAKEN_API_SECRET" cfg.credentials.apiSecret)
        ++ lib.optional (cfg.settings.restUrl != null) (valueLine "KRAKEN_REST_URL" cfg.settings.restUrl)
        ++ lib.optional (cfg.settings.wsUrl != null) (valueLine "KRAKEN_WS_URL" cfg.settings.wsUrl)
        ++ lib.optional (cfg.settings.wsAuthUrl != null) (valueLine "KRAKEN_WS_AUTH_URL" cfg.settings.wsAuthUrl)
        ++ lib.optional (cfg.settings.minInterval != null) (valueLine "KRAKEN_MIN_INTERVAL" cfg.settings.minInterval)
        ++ lib.optional (cfg.settings.paperMode != null) (valueLine "KRAKEN_PAPER_MODE" cfg.settings.paperMode)
        ++ lib.optional (cfg.settings.paperBalance != null) (valueLine "KRAKEN_PAPER_BALANCE" cfg.settings.paperBalance)
        ++ lib.concatLists (lib.mapAttrsToList
              (name: value: lib.optional (value != null) (valueLine name value))
              cfg.settings.extra);

      ob = cfg.recorders.orderBook;
    in
    {
      # Make the package available system-wide.
      environment.systemPackages = [ cfg.package ];

      # ── G1 order-book depth recorder ──────────────────────────────────────
      #
      # ⚠️ OPTION-NAMESPACE TRAP, and the reason this is a SYSTEM unit.
      # `nix/module.nix` is a **NixOS** module, so `systemd.user.*` — a
      # home-manager option — DOES NOT EVALUATE here.  Writing this as a user
      # timer would fail the host's evaluation outright, which is why the
      # funding pass had to ship a plain `systemd/user` unit file instead.
      # So: this module emits a SYSTEM service + timer, and the shipped
      # `systemd/kraken-trading-bot-order-book.*` files + `just depth-timer`
      # install the equivalent USER unit for hosts (including this one) that
      # run the recorder from a checkout rather than from this module.
      #
      # Either shape accumulates the same data; they are different install
      # paths, not different recorders.
      systemd.services."kraken-trading-bot-order-book" = lib.mkIf ob.enable {
        description = "Record one Kraken order-book depth snapshot (G1)";
        # KEYLESS endpoint: deliberately no After=/Requires= on the credential
        # oneshot.  /0/public/Depth needs no secret, and making it wait for
        # agenix materialisation would turn a working call into a silent hole.
        after = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          Type = "oneshot";
          # Runs the PACKAGE's own console script rather than `nix run <path>`:
          # this module already has the package built, and a path reference
          # here would both re-evaluate per fire and depend on a checkout
          # that a module consumer does not necessarily have.
          #
          # A LIST, not one joined string: `systemd.services.<name>.
          # serviceConfig.ExecStart` is typed `listOf str`, and passing a
          # string fails evaluation with "expected a list but found a string".
          # (Caught by evaluating the module, not by reading it.)
          ExecStart = [
            "${cfg.package}/bin/kraken-trading-bot"
            "record-depth"
            "--pair"
            ob.pair
            "--output"
            ob.output
            "--count"
            (toString ob.count)
            "--expected-interval-seconds"
            (toString ob.expectedIntervalSeconds)
            "--gap-factor"
            (toString ob.gapFactor)
          ] ++ lib.optional (ob.statusOutput != null) "--status-output"
            ++ lib.optional (ob.statusOutput != null) ob.statusOutput;
          TimeoutStartSec = 300;
          # Creates /var/lib/kraken-trading-bot, so `output`'s default
          # parent exists on a fresh activation.
          StateDirectory = "kraken-trading-bot";
        };
      };

      systemd.timers."kraken-trading-bot-order-book" = lib.mkIf ob.enable {
        description = "Hourly Kraken order-book depth snapshot (G1)";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnCalendar = ob.onCalendar;
          # Catch up after downtime instead of silently skipping hours.
          # `record-depth` is append-only, so a catch-up cannot damage what is
          # already there, and the gap counter still sees the hole.
          Persistent = true;
          RandomizedDelaySec = 120;
Unit = "kraken-trading-bot-order-book.service";
          };
        };

        # ── weekly off-machine checkpoint ──────────────────────────────────
        # The depth log is UNRECOVERABLE — Kraken has no historical Depth
        # endpoint, so a lost file is lost data.  `Persistent = true` covers
        # missed FIRES, not a lost disk.  This copies it off the machine.
        #
        # Weekly, not daily: at one record per hour a daily full copy is 11.8 GB
        # a year, against 129 MB for a rolling file plus weekly incrementals.
        #
        # `checkpointOnCalendar` is a Monday morning so the copy is early in the
        # week, leaving time to notice a failure before the next one.
        systemd.services."kraken-trading-bot-depth-checkpoint" = lib.mkIf ob.checkpoint.enable {
          description = "Copy the order-book depth log off this machine (G1)";
          wantedBy = [ "multi-user.target" ];
serviceConfig = {
              Type = "oneshot";
              # The repo path is fixed at build time; the recipe refuses any
              # destination on the same physical disk as the log.
              WorkingDirectory = cfg.homeDir;
              ExecStart = "${pkgs.just}/bin/just --justfile ${cfg.homeDir}/Projects/kraken-trading-bot/justfile --working-directory ${cfg.homeDir}/Projects/kraken-trading-bot depth-backup-git ${ob.checkpoint.remote}";
              # A dedicated deploy key on kraken-depth-archive alone, NOT a PAT and
              # not id_ed25519 — a deploy key cannot reach any other repo, which a
              # PAT or a registered personal key can.  IdentitiesOnly=yes stops an
              # agent key from standing in for it, so the scoping is not merely
              # nominal.  Pinned per-unit: the credential is a property of THIS
              # unit, not of whoever happens to be logged in.
              Environment = [
                "HOME=${cfg.homeDir}"
                "GIT_SSH_COMMAND=ssh -i ${cfg.homeDir}/.ssh/kraken-depth-archive -o IdentitiesOnly=yes -o BatchMode=yes"
              ];
              # Network and DNS may be down; a failed week must be visible in the
              # journal, not fatal to boot.
              SuccessExitStatus = 0;
            };
        };
        systemd.timers."kraken-trading-bot-depth-checkpoint" = lib.mkIf ob.checkpoint.enable {
          description = "Weekly off-machine copy of the order-book depth log (G1)";
          wantedBy = [ "timers.target" ];
          timerConfig = {
            OnCalendar = ob.checkpoint.onCalendar;
            Persistent = true;
            RandomizedDelaySec = 600;
            Unit = "kraken-trading-bot-depth-checkpoint.service";
          };
        };

      # Write a credentials file if individual options are provided.
      # The file is mode 0600 and owned by root, loadable by systemd and
      # by the app's dotenv loader.  Keyfile values are resolved here via
      # `cat`, so secrets only ever exist in /run and never in the store.
      systemd.services."kraken-trading-bot-env" = lib.mkIf writeEnv {
        description = "Write kraken-trading-bot credentials";
        wantedBy = [ "multi-user.target" ];
        after = cfg.credentials.after;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script = ''
          set -euo pipefail
          mkdir -p ${builtins.dirOf cfg.envFilePath}
          {
          ${lib.concatMapStringsSep "\n" (line: "  " + line)
            ([ "echo '# generated by services.kraken-trading-bot - do not edit'" ] ++ envLines)}
          } > ${cfg.envFilePath}
          chmod 0600 ${cfg.envFilePath}
        '';
      };
    }
  );
}
