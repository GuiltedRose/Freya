# A real, non-voting replay node. Disabled and manual-start by default.
{ config, lib, pkgs, nodePackage ? pkgs.emptyDirectory, ... }:
let
  cfg = config.services.crypto-bot-node;
  storageGuard = "${pkgs.python3}/bin/python ${../node_storage_guard.py} --minimum-bytes ${toString cfg.storageFreeMinimumBytes} "
    + lib.concatMapStringsSep " " (path: "--path ${lib.escapeShellArg path}")
      [ cfg.stateDirectory "${cfg.stateDirectory}/ledger" "${cfg.stateDirectory}/accounts" "${cfg.stateDirectory}/snapshots" ];
  args = [
    "--no-voting" "--private-rpc" "--full-rpc-api"
    "--enable-rpc-transaction-history" "--rpc-bind-address" "127.0.0.1"
    "--rpc-port" "8899" "--dynamic-port-range" "${toString cfg.peerPortStart}-${toString cfg.peerPortEnd}"
    "--expected-genesis-hash" "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
    "--only-known-rpc" "--ledger" "${cfg.stateDirectory}/ledger"
    "--accounts" "${cfg.stateDirectory}/accounts" "--snapshots" "${cfg.stateDirectory}/snapshots"
    "--maximum-full-snapshots-to-retain" "${toString cfg.retention.fullSnapshots}"
    "--maximum-incremental-snapshots-to-retain" "${toString cfg.retention.incrementalSnapshots}"
    "--no-xdp" "--snapshot-archive-format" "zstd"
    "--snapshot-zstd-compression-level" "${toString cfg.snapshotCompressionLevel}"
    "--rocksdb-ledger-compression" "zlib"
    "--accounts-index-limit" cfg.accountsIndexLimit
    "--accounts-db-write-cache-limit" cfg.accountsWriteCache
    "--accounts-db-read-cache-limit" cfg.accountsReadCache
    "--log" "-"
  ] ++ lib.concatMap (value: [ "--entrypoint" value ]) cfg.entrypoints
    ++ lib.concatMap (value: [ "--known-validator" value ]) cfg.knownValidators;
in {
  imports = [ ./solana-network-isolation.nix ];
  options.services.crypto-bot-node = {
    enable = lib.mkEnableOption "self-hosted mainnet replay RPC node within the existing transfer cap";
    package = lib.mkOption { type = lib.types.package; default = nodePackage; description = "Pinned Agave release; override only with a reviewed compatible package."; };
    identityFile = lib.mkOption { type = lib.types.str; description = "External node identity, distinct from the trading wallet; loaded as a private credential."; };
    stateDirectory = lib.mkOption { type = lib.types.str; default = "/var/lib/crypto-bot-node"; };
    entrypoints = lib.mkOption { type = lib.types.listOf lib.types.str; default = []; };
    knownValidators = lib.mkOption { type = lib.types.listOf lib.types.str; default = []; };
    peerPortStart = lib.mkOption { type = lib.types.port; default = 8000; };
    peerPortEnd = lib.mkOption { type = lib.types.port; default = 8026; };
    memoryHigh = lib.mkOption { type = lib.types.str; default = "48G"; };
    memoryMax = lib.mkOption { type = lib.types.str; default = "56G"; };
    snapshotCompressionLevel = lib.mkOption { type = lib.types.ints.between 1 19; default = 15; };
    accountsIndexLimit = lib.mkOption { type = lib.types.enum [ "25GB" "50GB" "100GB" "200GB" "unlimited" ]; default = "25GB"; };
    accountsWriteCache = lib.mkOption { type = lib.types.str; default = "1GiB"; };
    accountsReadCache = lib.mkOption { type = lib.types.str; default = "1GiB,2GiB"; };
    storageFreeMinimumBytes = lib.mkOption { type = lib.types.ints.positive; default = 107374182400;
      description = "Free-space reserve per node filesystem. Stops node instead of deleting history; allow room for writes between checks."; };
    retention = {
      fullSnapshots = lib.mkOption {
        type = lib.types.ints.between 2 1024;
        default = 3;
        description = "Full snapshot archives retained by Agave's native cleanup. Does not prune transaction history or bot records.";
      };
      incrementalSnapshots = lib.mkOption {
        type = lib.types.ints.between 4 65536;
        default = 8;
        description = "Incremental snapshot archives retained by Agave's native cleanup; never delete files with an external age-based job.";
      };
    };
    capacityReviewed = lib.mkOption { type = lib.types.bool; default = false; description = "Operator has reviewed storage, RAM, release requirements and peer bandwidth; not proof of successful replay."; };
  };
  config = lib.mkIf cfg.enable {
    # Agave's startup checks remain enabled. Apply its documented host minima
    # declaratively; operators' explicit host settings still take precedence.
    boot.kernel.sysctl = {
      "net.core.rmem_max" = lib.mkDefault 134217728;
      "net.core.wmem_max" = lib.mkDefault 134217728;
      "vm.max_map_count" = lib.mkDefault 1000000;
    };
    assertions = [
      { assertion = cfg.package != pkgs.emptyDirectory; message = "No bundled Agave package for this host; provide a reviewed compatible node package."; }
      { assertion = cfg.capacityReviewed; message = "Review actual replay resources before enabling the node; no runtime measurements are inferred."; }
      { assertion = cfg.entrypoints != [] && cfg.knownValidators != []; message = "Explicit reviewed mainnet bootstrap peers are required."; }
      { assertion = lib.hasPrefix "/" cfg.identityFile && !(lib.hasPrefix "/nix/store/" cfg.identityFile); message = "Node identity must be an external runtime secret."; }
      { assertion = cfg.stateDirectory != "/" && lib.hasPrefix "/" cfg.stateDirectory && !(lib.hasPrefix "/nix/store/" cfg.stateDirectory)
          && !(lib.hasInfix "/../" cfg.stateDirectory); message = "Node state must be a dedicated external persistent directory."; }
      { assertion = cfg.peerPortStart >= 1024 && cfg.peerPortEnd-cfg.peerPortStart >= 26 && cfg.peerPortEnd-cfg.peerPortStart <= 255
          && !(cfg.peerPortStart <= 8900 && cfg.peerPortEnd >= 8899); message = "Agave needs a peer range spanning at least 26 ports; keep it unprivileged, bounded and separate from private RPC/WS ports."; }
    ];
    services.solana-budget-isolation = {
      enable = true;
      services = [ "crypto-bot-node" ];
      peerPorts = { start = cfg.peerPortStart; end = cfg.peerPortEnd; };
    };
    users.groups.crypto-bot-node = {};
    users.users.crypto-bot-node = { isSystemUser = true; group = "crypto-bot-node"; };
    systemd.tmpfiles.rules = map (path: "d ${path} 0700 crypto-bot-node crypto-bot-node -")
      [ cfg.stateDirectory "${cfg.stateDirectory}/ledger" "${cfg.stateDirectory}/accounts" "${cfg.stateDirectory}/snapshots" ];
    systemd.services.crypto-bot-node = {
      description = "Private self-hosted Solana replay RPC; 120/150 GB bounded session";
      wantedBy = [];
      restartIfChanged = false;
      bindsTo = [ "crypto-bot-node-storage-guard.service" ];
      after = [ "crypto-bot-node-storage-guard.service" ];
      unitConfig.RequiresMountsFor = [ cfg.stateDirectory "${cfg.stateDirectory}/ledger" "${cfg.stateDirectory}/accounts" "${cfg.stateDirectory}/snapshots" ];
      serviceConfig = {
        ExecStartPre = "${storageGuard} --once";
        User = "crypto-bot-node";
        Group = "crypto-bot-node";
        WorkingDirectory = cfg.stateDirectory;
        LoadCredential = "identity.json:${cfg.identityFile}";
        ReadWritePaths = [ cfg.stateDirectory ];
        PrivateTmp = true;
        UMask = "0077";
        LimitNOFILE = 1000000;
        LimitMEMLOCK = 2000000000;
        MemoryHigh = cfg.memoryHigh;
        MemoryMax = cfg.memoryMax;
        MemorySwapMax = 0;
        TimeoutStopSec = 120;
        KillSignal = "SIGINT";
      };
      script = ''
        exec ${cfg.package}/bin/agave-validator --identity "$CREDENTIALS_DIRECTORY/identity.json" ${lib.escapeShellArgs args}
      '';
    };
    systemd.services.crypto-bot-node-storage-guard = {
      description = "Node disk reserve monitor; never prunes blockchain or bot history";
      wantedBy = [];
      partOf = [ "crypto-bot-node.service" ];
      unitConfig.RequiresMountsFor = [ cfg.stateDirectory "${cfg.stateDirectory}/ledger" "${cfg.stateDirectory}/accounts" "${cfg.stateDirectory}/snapshots" ];
      serviceConfig = {
        Type = "exec";
        User = "crypto-bot-node";
        ExecStart = storageGuard;
        Restart = "no";
        NoNewPrivileges = true;
        PrivateNetwork = true;
        ProtectSystem = "strict";
        ProtectHome = true;
      };
    };
    # RPC stays inside the shared namespace. No public 8899/8900 firewall openings.
    systemd.services.crypto-bot-indexer = lib.mkIf (lib.attrByPath [ "services" "crypto-bot-paper" "enable" ] false config
        && lib.attrByPath [ "services" "crypto-bot-paper" "rpcMode" ] "remote" config != "remote") {
      after = [ "crypto-bot-node.service" ];
      wants = [ "crypto-bot-node.service" ];
    };
  };
}
