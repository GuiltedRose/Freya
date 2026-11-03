# Private selective-data bridge. No service is automatically activated.
{ config, lib, pkgs, package, ... }:
let cfg = config.services.freya-node; in {
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
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      { assertion = cfg.user != "" && cfg.user != "root"; message = "An existing non-root collector user is required."; }
    ] ++ map (path: {
      assertion = lib.hasPrefix "/" path && path != "/" && !(lib.hasPrefix "/nix/store" path) && !(lib.hasInfix "/../" path);
      message = "Native node paths must be explicit external absolute paths.";
    }) [ cfg.configFile cfg.externalConfigFile cfg.providerLedger cfg.stateDirectory ];
    services.solana-budget-isolation = lib.mkIf (cfg.networkMode == "isolated") {
      enable = true; services = [ "freya-node" ];
    };
    systemd.tmpfiles.rules = [ "d ${cfg.stateDirectory} 0700 ${cfg.user} ${cfg.group} -" ];
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
