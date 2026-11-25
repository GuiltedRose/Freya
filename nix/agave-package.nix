# Build a reviewed, pinned Agave source with its matching Rust toolchain/vendor hash.
# No release installer, mutable download, test execution or node startup.
{ pkgs, src, version, cargoHash ? null, rustPlatform ? pkgs.rustPlatform
, replayOnly ? false }:
rustPlatform.buildRustPackage ({
  pname = if replayOnly then "freya-agave-replay" else "crypto-bot-agave";
  inherit src version;
  nativeBuildInputs = [ pkgs.pkg-config pkgs.cmake pkgs.protobuf pkgs.perl pkgs.rustPlatform.bindgenHook ];
  buildInputs = [ pkgs.openssl pkgs.udev pkgs.bzip2 pkgs.zlib pkgs.lz4 ];
  cargoBuildFlags = if replayOnly then [ "--package" "agave-ledger-tool" ]
    else [ "--package" "agave-validator" "--package" "solana-keygen" ];
  PROTOC = "${pkgs.protobuf}/bin/protoc";
  doCheck = false; # Operator runs upstream validation on the provisioned node host.
  meta = {
    platforms = [ "x86_64-linux" ];
    mainProgram = if replayOnly then "agave-ledger-tool" else "agave-validator";
  };
} // (if cargoHash != null then { inherit cargoHash; } else {
  cargoLock = { lockFile = "${src}/Cargo.lock"; allowBuiltinFetchGit = true; };
}))
