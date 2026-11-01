{ config, lib, pkgs, package }:
let
  cfg = config.services.crypto-bot-paper;
  inherit (lib) mkOption mkEnableOption types mkIf escapeShellArg;
  state = cfg.stateDirectory;
  index = "${state}/index/index.sqlite";
  common = {
    wantedBy = [];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment = {
      PYTHONUNBUFFERED = "1";
      PYTHONDONTWRITEBYTECODE = "1";
      CRYPTO_BOT_PROVIDER_LIMITS = "${state}/provider_limits.sqlite";
    };
    serviceConfig = {
      User = "crypto-bot-paper";
      Group = "crypto-bot-paper";
      WorkingDirectory = state;
      Restart = "on-failure";
      RestartSec = 30;
      UMask = "0077";
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      RestrictSUIDSGID = true;
      CapabilityBoundingSet = "";
      ReadWritePaths = [ state ];
      TimeoutStopSec = 45;
    };
    # Bound systemd restart loops as well as the application's reconnect attempts.
    startLimitIntervalSec = 600;
    startLimitBurst = 5;
  };
in {
  imports = [ ./solana-network-isolation.nix ];
  options.services.crypto-bot-paper = {
    enable = mkEnableOption "local read-only indexer deployment (no live service)";
    package = mkOption { type = types.package; default = package; };
    stateDirectory = mkOption {
      type = types.str; default = "/var/lib/crypto-bot-paper";
      description = "External persistent state; never put this under /nix/store.";
    };
    indexerConfigFile = mkOption {
      type = types.str;
      description = "Runtime-only absolute config path; loaded as a systemd credential. Never use a Nix path literal or builtins.readFile for secrets.";
    };
    startSlot = mkOption { type = types.nullOr types.ints.unsigned; default = null; };
    batchSlots = mkOption { type = types.ints.between 1 64; default = 8; };
    maxCatchupSlots = mkOption { type = types.ints.between 1 64; default = 64; };
    learning = {
      enable = mkEnableOption "bounded offline learning and active qualified model decisions";
      configFile = mkOption { type = types.nullOr types.str; default = null; };
    };
    live = {
      enable = mkEnableOption "manually started, explicitly authorized live momentum reclaim trading";
      environmentFile = mkOption { type = types.str; default = ""; description = "Runtime SOLANA_RPC_URL environment file, outside the Nix store."; };
      keypairFile = mkOption { type = types.str; default = ""; description = "Runtime signer file loaded through systemd credentials; never stored in Nix."; };
    };
    paper = {
      enable = mkEnableOption "separate isolated paper service";
      rulesFile = mkOption { type = types.str; default = "${cfg.package}/share/crypto-bot/meme_live_rules.json"; };
      watchlistFile = mkOption { type = types.nullOr types.str; default = null; };
      # Initial capital is required explicitly, and is ignored on normal resumes.
      initialBalanceUsd = mkOption { type = types.number; default = 6; };
      initialNativeSol = mkOption { type = types.nullOr types.number; default = null; };
      initialSolPriceUsd = mkOption { type = types.nullOr types.number; default = null; };
      mints = mkOption { type = types.listOf types.str; default = []; };
    };
  };
  config = mkIf cfg.enable {
    assertions = [
      { assertion = lib.hasPrefix "/" state && !(lib.hasPrefix "/nix/store/" state);
        message = "crypto-bot stateDirectory must be an external absolute path."; }
      { assertion = lib.hasPrefix "/" cfg.indexerConfigFile && !(lib.hasPrefix "/nix/store/" cfg.indexerConfigFile);
        message = "Indexer credentials/config must be a runtime path outside the Nix store."; }
      { assertion = cfg.maxCatchupSlots >= cfg.batchSlots;
        message = "maxCatchupSlots must be at least batchSlots."; }
      { assertion = !cfg.paper.enable || (cfg.paper.initialNativeSol != null && cfg.paper.initialNativeSol >= 0
          && cfg.paper.initialSolPriceUsd != null && cfg.paper.initialSolPriceUsd > 0);
        message = "Paper requires explicit virtual SOL inventory and initial SOL/USD valuation; existing balances are never changed."; }
      { assertion = !cfg.learning.enable || (cfg.learning.configFile != null && lib.hasPrefix "/" cfg.learning.configFile);
        message = "Learning requires an absolute reviewed runtime config file."; }
      { assertion = !cfg.live.enable || (lib.hasPrefix "/" cfg.live.environmentFile && lib.hasPrefix "/" cfg.live.keypairFile
          && !(lib.hasPrefix "/nix/store/" cfg.live.keypairFile) && !(lib.hasPrefix "/nix/store/" cfg.live.environmentFile));
        message = "Live activation requires explicit external runtime environment and signer paths."; }
    ];
    services.solana-budget-isolation.enable = true;
    services.solana-budget-isolation.services = [ "crypto-bot-indexer" ] ++ lib.optional cfg.paper.enable "crypto-bot-paper" ++ lib.optional cfg.live.enable "crypto-bot-live";
    users.groups.crypto-bot-paper = {};
    users.users.crypto-bot-paper = {
      isSystemUser = true;
      group = "crypto-bot-paper";
    };
    systemd.tmpfiles.rules = [
      "d ${state} 0700 crypto-bot-paper crypto-bot-paper -"
      "d ${state}/index 0700 crypto-bot-paper crypto-bot-paper -"
      "d ${state}/paper 0700 crypto-bot-paper crypto-bot-paper -"
      "d ${state}/ml 0700 crypto-bot-paper crypto-bot-paper -"
      "d ${state}/live 0700 crypto-bot-paper crypto-bot-paper -"
      "d ${state}/state 0700 crypto-bot-paper crypto-bot-paper -"
    ];
    systemd.services.crypto-bot-indexer = lib.recursiveUpdate common {
      description = "Finalized Solana Pump indexer (read-only RPC)";
      serviceConfig.LoadCredential = [ "indexer.json:${cfg.indexerConfigFile}" ];
      script = ''
        ${cfg.package}/bin/crypto-bot-health --config "$CREDENTIALS_DIRECTORY/indexer.json"
        exec ${cfg.package}/bin/crypto-bot-indexer \
          --config "$CREDENTIALS_DIRECTORY/indexer.json" \
          --db ${escapeShellArg index} --loop \
          --batch-slots ${toString cfg.batchSlots} --max-catchup-slots ${toString cfg.maxCatchupSlots} \
          ${lib.optionalString (cfg.startSlot != null) "--start-slot ${toString cfg.startSlot}"}
      '';
    };
    systemd.services.crypto-bot-paper = mkIf cfg.paper.enable (lib.recursiveUpdate common {
      description = "Isolated local-index paper trader (no signing)";
      after = common.after ++ [ "crypto-bot-indexer.service" ];
      wants = common.wants ++ [ "crypto-bot-indexer.service" ];
      serviceConfig.LoadCredential = lib.optional cfg.learning.enable "learning.json:${cfg.learning.configFile}";
      script = ''
        exec ${cfg.package}/bin/crypto-bot-paper \
          --state-dir ${escapeShellArg "${state}/paper"} --onchain-db ${escapeShellArg index} \
          --rules ${escapeShellArg cfg.paper.rulesFile} --exploratory \
          --balance ${escapeShellArg (toString cfg.paper.initialBalanceUsd)} \
          --native-sol ${escapeShellArg (toString cfg.paper.initialNativeSol)} --sol-price ${escapeShellArg (toString cfg.paper.initialSolPriceUsd)} \
          --discover --loop --interval 30 --no-auto-reset \
          ${lib.optionalString cfg.learning.enable "--unified-db ${escapeShellArg "${state}/ml/unified.sqlite"} --unified-config \"$CREDENTIALS_DIRECTORY/learning.json\""} \
          ${lib.optionalString (cfg.paper.watchlistFile != null) "--watchlist ${escapeShellArg cfg.paper.watchlistFile}"} \
          ${lib.concatMapStringsSep " " (mint: "--mint ${escapeShellArg mint}") cfg.paper.mints}
      '';
    });
    systemd.services.crypto-bot-unified-paper-learning = mkIf cfg.learning.enable {
      description = "Bounded offline paper/live ledger learning; no order authority";
      serviceConfig = {
        Type = "oneshot";
        User = "crypto-bot-paper";
        WorkingDirectory = state;
        LoadCredential = [ "learning.json:${cfg.learning.configFile}" ];
        PrivateNetwork = true;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "${state}/ml" ];
        UMask = "0077";
        CPUQuota = "25%";
        MemoryMax = "1G";
        MemorySwapMax = 0;
        TimeoutStartSec = 300;
      };
      script = ''
        initialization=""
        if [ ! -e ${escapeShellArg "${state}/ml/unified.sqlite"} ]; then initialization="--initialize"; fi
        exec ${cfg.package}/bin/crypto-bot-unified-learning --db ${escapeShellArg "${state}/ml/unified.sqlite"} \
          $initialization --pipeline-config "$CREDENTIALS_DIRECTORY/learning.json"
      '';
    };
    systemd.services.crypto-bot-live = mkIf cfg.live.enable (lib.recursiveUpdate common {
      description = "Single-strategy live momentum reclaim; manual authorization";
      serviceConfig = {
        EnvironmentFile = cfg.live.environmentFile;
        LoadCredential = [ "wallet.json:${cfg.live.keypairFile}" ] ++ lib.optional cfg.learning.enable "learning.json:${cfg.learning.configFile}";
      };
      script = ''
        ${cfg.package}/bin/crypto-bot-health --db ${escapeShellArg index} --require-ready
        unset SOLANA_PRIVATE_KEY
        export SOLANA_KEYPAIR_PATH="$CREDENTIALS_DIRECTORY/wallet.json"
        exec ${cfg.package}/bin/crypto-bot-live --execute --onchain-db ${escapeShellArg index} \
          --state-dir ${escapeShellArg "${state}/live"} --rules ${escapeShellArg cfg.paper.rulesFile} \
          --learning-paper-db ${escapeShellArg "${state}/paper/paper.sqlite"} \
          --wallet-source-dir ${escapeShellArg "${state}/wallet_watch"} --neural-mode qualified --loop \
          ${lib.optionalString cfg.learning.enable "--unified-db ${escapeShellArg "${state}/ml/unified.sqlite"} --unified-config \"$CREDENTIALS_DIRECTORY/learning.json\""} \
          ${lib.optionalString (cfg.paper.watchlistFile != null) "--watchlist ${escapeShellArg cfg.paper.watchlistFile}"} \
          ${lib.concatMapStringsSep " " (mint: "--mint ${escapeShellArg mint}") cfg.paper.mints}
      '';
    });
    systemd.timers.crypto-bot-unified-paper-learning = mkIf cfg.learning.enable {
      timerConfig = { OnActiveSec = "1m"; OnUnitInactiveSec = "15m"; };
    };
    systemd.services.crypto-bot-health = {
      description = "Read-only market pipeline readiness";
      serviceConfig = {
        Type = "oneshot";
        User = "crypto-bot-paper";
        PrivateNetwork = true;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
      };
      script = ''
        exec ${cfg.package}/bin/crypto-bot-health --db ${escapeShellArg index} --require-ready \
          ${lib.concatMapStringsSep " " (mint: "--mint ${escapeShellArg mint}") cfg.paper.mints}
      '';
    };
    systemd.timers.crypto-bot-health.timerConfig = { OnActiveSec = "30s"; OnUnitInactiveSec = "1m"; };
  };
}
