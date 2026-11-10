"""Prepare a conventional NixOS add-on; never install, activate, grant or probe."""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import subprocess
from native_mainnet_preflight import default_uplink
from provider_limits import provider_limits_path

ROOT = Path(__file__).resolve().parent


class PreparationError(ValueError):
    pass


def capture(*args):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def nix_string(value):
    return json.dumps(str(value), ensure_ascii=False).replace('${', '\\${')


def private_file(path):
    if path.is_symlink() or not path.is_file():
        raise PreparationError('Missing/nonregular private runtime file: ' + str(path))
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise PreparationError('Runtime file must already be owned privately by the collector: ' + str(path))


def prepare(args):
    if os.getuid() == 0:
        raise PreparationError('Run as the existing non-root collector, not sudo')
    if not Path('/etc/nixos/configuration.nix').is_file():
        raise PreparationError('Run on Monolith with its existing /etc/nixos/configuration.nix')
    if args.output.absolute().exists():
        raise PreparationError('Output already exists; preserve it or select another --output')
    external = args.config.absolute()
    if not external.exists():
        raise PreparationError('Missing private RPC configuration; for confirmed runtime data loss see docs/native_fresh_start.md')
    private_file(external)
    from pipeline_runtime import read_json, resolve_indexer
    from runtime_health import validate_config
    config = resolve_indexer(read_json(external), {'SOLANA_RPC_URL': os.environ.get('SOLANA_RPC_URL', '')})
    validate_config(config)
    endpoints = [e for e in config.get('endpoints', []) if e.get('enabled') is True]
    if len(endpoints) != 1 or endpoints[0].get('backend') != 'rust':
        raise PreparationError('One existing reviewed Rust RPC source is required')
    # Static policy/boundary checks only; make no RPC requests and grant no rights.
    from solana_rpc_compat import check_boundary
    check_boundary(endpoints[0])
    ledger = provider_limits_path(args.index_db)
    if not ledger.exists():
        raise PreparationError('Missing provider ledger; startup never resets accounting. For explicit fresh bootstrap see docs/native_fresh_start.md')
    private_file(ledger)
    if not os.access(ledger, os.W_OK) or not os.access(ledger.parent, os.W_OK):
        raise PreparationError('Collector must already be able to update the existing shared provider ledger')
    uplink = default_uplink()
    for unit in ('freya-peer-discovery.service','freya-node.service','solana-budget-network.service'):
        state = capture('systemctl','show',unit,'--property=LoadState','--value')
        if args.refresh:
            if state != 'loaded':raise PreparationError('Refresh requires the already installed Freya services')
            active=capture('systemctl','show',unit,'--property=ActiveState','--value')
            if active not in ('inactive','failed'):
                raise PreparationError('Wait for the existing session to finish before refreshing: '+unit)
            if unit != 'solana-budget-network.service':
                owner=capture('systemctl','show',unit,'--property=User','--value')
                if owner != pwd.getpwuid(os.getuid()).pw_name:
                    raise PreparationError('Installed collector identity differs; preserve configuration for review')
                import shlex
                paths=shlex.split(capture('systemctl','show',unit,'--property=ReadWritePaths','--value'))
                if str(ledger.parent) not in paths:
                    raise PreparationError('Installed shared-ledger mapping differs; explicit review required')
        elif state != 'not-found':
            raise PreparationError('Existing service requires configuration review, not replacement: ' + unit)
    # Build only; the operator runs this preparation command explicitly.
    print('Building the pinned node package; no service will start.', flush=True)
    package = capture('nix','build','--no-write-lock-file','--no-link','--print-out-paths','.#native-node')
    if not package.startswith('/nix/store/') or '\n' in package:
        raise PreparationError('Unexpected package output')
    metadata = json.loads(capture('nix','flake','metadata','--no-write-lock-file','--json','.'))
    source = metadata['path']
    if not source.startswith('/nix/store/'):
        raise PreparationError('Expected immutable Git-backed source snapshot')
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(pwd.getpwuid(os.getuid()).pw_gid).gr_name
    output = args.output.absolute()
    if output.exists():
        raise PreparationError('Preparation output already exists; preserve it or select another --output')
    output.mkdir(mode=0o700, parents=True)
    def write(name, value):
        path = output/name
        with os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            stream.write(value)
    write('follow.json', json.dumps({'bootstrap_peers':[], 'continuous':False,'duration_seconds':40,
        'max_namespace_bytes':1000000,'max_disk_bytes':67108864,'max_shreds_per_slot':1024,
        'max_attempts_per_window':128,'request_interval_ms':500},indent=2)+'\n')
    write('node.json', json.dumps({'mode':'external','external_config':str(external),
        'network_mode':endpoints[0].get('network_mode','isolated'),'provider_ledger':str(ledger)},indent=2)+'\n')
    module = '''{ config, lib, pkgs, ... }: {
  imports = [ ({ config, lib, pkgs, ... }: import SOURCE/nix/selective-node.nix {
    inherit config lib pkgs;
    package = PACKAGE;
  }) ];
  # Explicit operator root override; review this membership before activation.
  services.freya-operators = { enable = true; members = [ USER ]; };
  services.freya-node = {
    enable = true;
    user = USER;
    group = GROUP;
    configFile = NODECONFIG;
    externalConfigFile = EXTERNAL;
    providerLedger = LEDGER;
    stateDirectory = "/var/lib/freya-node";
    networkMode = "isolated";
    peerOperation = "follow";
    peerDiscoveryConfig = FOLLOW;
    continuousMode = false;
  };
  systemd.services.freya-node.environment.FREYA_NODE_NETWORK_MODE = lib.mkForce RPCMODE;
  HOMEACCESS
  services.solana-budget-isolation = {
    uplinkInterface = UPLINK;
    ledgerFile = "/var/lib/solana-network/budget.sqlite";
    sessionBytes = 1000000;
    sessionSeconds = 55;
    uploadKbytes = 16;
    downloadKbytes = 16;
    reserveSession = false;
    peerPorts = null;
    calendarMonthly = true;
  };
}
'''
    # Replace token names once; paths remain strings, source/package are Nix store paths.
    import re
    home_access = ''
    if any(ledger.is_relative_to(base) for base in (Path('/home'),Path('/root'),Path('/run/user'))):
        for service in ('freya-node','freya-peer-discovery'):
            home_access += f'systemd.services.{service}.serviceConfig = {{ ProtectHome = lib.mkForce \"tmpfs\"; BindPaths = [ {nix_string(ledger.parent)} ]; }};\n'
    values = {'HOMEACCESS':home_access,'SOURCE':source,'PACKAGE':package,'USER':nix_string(user),'GROUP':nix_string(group),
              'NODECONFIG':nix_string(output/'node.json'),'EXTERNAL':nix_string(external),
              'LEDGER':nix_string(ledger),'FOLLOW':nix_string(output/'follow.json'),'UPLINK':nix_string(uplink),'RPCMODE':nix_string(endpoints[0].get('network_mode','isolated'))}
    module = re.sub(r'\b('+'|'.join(values)+r')\b',lambda match:values[match[0]],module)
    write('freya-mainnet.nix',module)
    write('freya-diagnostic-host.nix','{ ... }: { imports = [ ./configuration.nix ./freya-mainnet.nix ]; }\n')
    print(json.dumps({'prepared':str(output),'uplink':uplink,'collector':user,'group':group,
                     'provider_ledger':str(ledger),'services_started':False,'network_probes':False},indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'onchain_pipeline.json')
    parser.add_argument('--index-db',type=Path,default=ROOT/'state/onchain/index.sqlite')
    parser.add_argument('--output',type=Path,default=ROOT/'state/mainnet-host-setup')
    parser.add_argument('--refresh',action='store_true',help='Prepare an update for matching, inactive installed Freya services')
    try:
        prepare(parser.parse_args())
    except PreparationError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        # Config/provider errors must never print URLs or credential contents.
        raise SystemExit('Host preparation stopped ('+type(error).__name__+'); no service was installed or started') from None


if __name__=='__main__':main()
