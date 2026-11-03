# Private selective-data bridge. No service is automatically activated.
{ config, lib, pkgs, package, ... }:
let
  cfg = config.services.freya-node;
  peerState = "${cfg.stateDirectory}/${if cfg.peerOperation == "discover" then "peer" else cfg.peerOperation}";
in {
  imports = [ ./solana-network-isolation.nix ];
  options.services.freya-node = {
    enable = lib.mkEnableOption "Freya selective acquisition (not an independent validator)";
    configFile = lib.mkOption { type = lib.types.str; };
    externalConfigFile = lib.mkOption { type = lib.types.str; };
    providerLedger = lib.mkOption { type = lib.types.str; };
    user = lib.mkOption { type = lib.types.str; description = "Existing non-root collector UID with access to the same provider ledger."; };
    group = lib.mkOption { type = lib.types.str; default = cfg.user; };
    stateDirectory = lib.mkOption { type = lib.types.str; default = "/var/lib/freya-node"; };
    networkMode = lib.mkOption { type = lib.types.enum [ "remote_provider" "isolated" ]; default = "remote_provider"; };
    peerDiscoveryConfig = lib.mkOption {
      type = lib.types.nullOr lib.types.str; default = null;
      description = "Private absolute discovery configuration; enables a manual, bounded diagnostic only.";
    };
    peerOperation = lib.mkOption {
      type = lib.types.enum [ "discover" "acquire" "follow" ]; default = "discover";
      description = "Manual diagnostic operation; acquisition requires an explicitly authorized UDP sender/port.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      { assertion = cfg.user != "" && cfg.user != "root"; message = "An existing non-root collector user is required."; }
      { assertion = cfg.peerDiscoveryConfig == null || (cfg.networkMode == "isolated" && lib.hasPrefix "/" cfg.peerDiscoveryConfig && !(lib.hasPrefix "/nix/store/" cfg.peerDiscoveryConfig));
        message = "Peer discovery requires isolated networking and a private absolute config path."; }
    ] ++ map (path: {
      assertion = lib.hasPrefix "/" path && path != "/" && !(lib.hasPrefix "/nix/store" path) && !(lib.hasInfix "/../" path);
      message = "Native node paths must be explicit external absolute paths.";
    }) [ cfg.configFile cfg.externalConfigFile cfg.providerLedger cfg.stateDirectory ];
    services.solana-budget-isolation = lib.mkIf (cfg.networkMode == "isolated") {
      enable = true; services = [ "freya-node" ] ++ lib.optional (cfg.peerDiscoveryConfig != null) "freya-peer-discovery";
    };
    systemd.tmpfiles.rules = [ "d ${cfg.stateDirectory} 0700 ${cfg.user} ${cfg.group} -" ]
      ++ lib.optional (cfg.peerDiscoveryConfig != null) "d ${peerState} 0700 ${cfg.user} ${cfg.group} -";
    systemd.services.freya-peer-discovery = lib.mkIf (cfg.peerDiscoveryConfig != null) {
      description = "Bounded Freya native gossip discovery; no trading admission";
      wantedBy = []; restartIfChanged = false; stopIfChanged = true;
      environment.CRYPTO_BOT_PROVIDER_LIMITS = cfg.providerLedger;
      serviceConfig = {
        User = cfg.user; Group = cfg.group; UMask = "0077";
        LoadCredential = [ "peer.json:${cfg.peerDiscoveryConfig}" "external.json:${cfg.externalConfigFile}" ];
        Restart = "no"; NoNewPrivileges = true; ProtectSystem = "strict";
        ProtectHome = true; PrivateTmp = true; CapabilityBoundingSet = "";
        MemoryAccounting = true; MemoryHigh = "1G"; MemoryMax = "2G"; MemorySwapMax = 0;
        TasksMax = 128; LimitNOFILE = 128; RuntimeMaxSec = 52; TimeoutStopSec = 3;
        ReadWritePaths = [ peerState (builtins.dirOf cfg.providerLedger) ];
      };
      script = ''
        export FREYA_NODE_EXTERNAL_CONFIG="$CREDENTIALS_DIRECTORY/external.json"
        exec ${package}/bin/freya-node --peer ${cfg.peerOperation} "$CREDENTIALS_DIRECTORY/peer.json" \
          ${lib.escapeShellArg peerState}
      '';
    };
    systemd.services.freya-node = {
      description = "Freya selective provider acquisition; provisional hints plus finalized reconciliation";
      wantedBy = []; restartIfChanged = false; stopIfChanged = true;
      environment = {
        CRYPTO_BOT_PROVIDER_LIMITS = cfg.providerLedger;
        FREYA_NODE_NETWORK_MODE = cfg.networkMode;
        PYTHONDONTWRITEBYTECODE = "1";
      };
      serviceConfig = {
        User = cfg.user; Group = cfg.group; UMask = "0077";
        LoadCredential = [ "node.json:${cfg.configFile}" "external.json:${cfg.externalConfigFile}" ];
        Restart = "no"; NoNewPrivileges = true; ProtectSystem = "strict"; ProtectHome = true;
        PrivateTmp = true; CapabilityBoundingSet = ""; MemoryAccounting = true;
        MemoryHigh = "3G"; MemoryMax = "4G"; MemorySwapMax = 0;
        ReadWritePaths = [ cfg.stateDirectory (builtins.dirOf cfg.providerLedger) ];
        TimeoutStopSec = 30;
      };
      script = ''
        export FREYA_NODE_EXTERNAL_CONFIG="$CREDENTIALS_DIRECTORY/external.json"
        exec ${package}/bin/freya-node --config "$CREDENTIALS_DIRECTORY/node.json" \
          --state-dir ${lib.escapeShellArg cfg.stateDirectory}
      '';
    };
  };
}
