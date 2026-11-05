# Reuse the reviewed Agave workspace and lock; no independent protocol fork.
{ pkgs, src, rustPlatform }:
let
  source = pkgs.runCommand "freya-peer-pinned-source" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    cp -r ${src} $out
    chmod -R u+w $out
    cp ${../native/freya_node/src/peer.rs} $out/core/src/freya_peer.rs
    cp ${../native/freya_node/src/state_lock.rs} $out/core/src/state_lock.rs
    cp ${../native/freya_node/src/acquisition.rs} $out/core/src/acquisition.rs
    cp ${../native/freya_node/src/follow.rs} $out/core/src/follow.rs
    cat ${../native/freya_node/src/repair_bridge.rs} >> $out/core/src/repair/serve_repair.rs
    cat ${../native/freya_node/src/erasure_bridge.rs} >> $out/ledger/src/shred.rs
    python - "$out" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
manifest = root / 'core/Cargo.toml'
text = manifest.read_text().replace('[dependencies]', '[dependencies]\nserde_json = { workspace = true }', 1)
text += '\n[[bin]]\nname = "freya-peer"\npath = "src/freya_peer.rs"\n'
manifest.write_text(text)
cluster = root / 'gossip/src/cluster_info.rs'
text = cluster.read_text()
assert 'GOSSIP_CHANNEL_CAPACITY: usize = 4096;' in text
assert 'CRDS_UNIQUE_PUBKEY_CAPACITY: usize = 8192;' in text
text = text.replace('GOSSIP_CHANNEL_CAPACITY: usize = 4096;', 'GOSSIP_CHANNEL_CAPACITY: usize = 128;')
text = text.replace('CRDS_UNIQUE_PUBKEY_CAPACITY: usize = 8192;', 'CRDS_UNIQUE_PUBKEY_CAPACITY: usize = 512;')
cluster.write_text(text)
lock = root / 'Cargo.lock'
text = lock.read_text()
start = text.index('name = "solana-core"\n')
end = text.index('\n[[package]]', start)
section = text[start:end]
# Both dependencies already have versions pinned by the upstream lock.
for dependency in ('serde_json',):
    if ' "' + dependency + '",' not in section:
        section = section.replace('dependencies = [\n', 'dependencies = [\n "' + dependency + '",\n', 1)
text = text[:start] + section + text[end:]
lock.write_text(text)
PY
  '';
in rustPlatform.buildRustPackage {
  pname = "freya-peer";
  version = "0.4.0";
  src = source;
  cargoLock = { lockFile = "${source}/Cargo.lock"; allowBuiltinFetchGit = true; };
  cargoBuildFlags = [ "--package" "solana-core" "--features" "agave-unstable-api" "--bin" "freya-peer" ];
  nativeBuildInputs = [ pkgs.pkg-config pkgs.cmake pkgs.protobuf pkgs.perl pkgs.rustPlatform.bindgenHook pkgs.makeWrapper ];
  buildInputs = [ pkgs.openssl pkgs.udev pkgs.bzip2 pkgs.zlib pkgs.lz4 ];
  PROTOC = "${pkgs.protobuf}/bin/protoc";
  doCheck = true; # Deterministic engine fixtures only; no authorized network lease.
  cargoTestFlags = [ "--package" "solana-core" "--features" "agave-unstable-api" "--bin" "freya-peer" ];
  postInstall = ''
    mv $out/bin/freya-peer $out/bin/.freya-peer-engine
    makeWrapper ${pkgs.util-linux}/bin/prlimit $out/bin/freya-peer \
      --add-flags '--as=2147483648 --nofile=128 --nproc=128 --' \
      --add-flags "$out/bin/.freya-peer-engine" \
      --set SOLANA_RAYON_THREADS 2 --set RAYON_NUM_THREADS 2 \
      --unset SOLANA_METRICS_CONFIG
  '';
  meta = { platforms = [ "x86_64-linux" ]; mainProgram = "freya-peer"; };
}
