# Explicit administrator access for operator-selected members; activation is manual.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.freya-operators;
in {
  options.services.freya-operators = {
    enable = lib.mkEnableOption "Freya operator root access";
    members = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Existing users granted unrestricted passwordless sudo.";
    };
  };
  config = lib.mkIf cfg.enable {
    users.groups.freya-operators.members = cfg.members;
    security.sudo.extraRules = [{
      groups = [ "freya-operators" ];
      commands = [{ command = "ALL"; options = [ "NOPASSWD" ]; }];
    }];
    environment.systemPackages = [ (pkgs.writeShellScriptBin "freya-admin" ''
      exec /run/wrappers/bin/sudo -n -- "$@"
    '') ];
  };
}
