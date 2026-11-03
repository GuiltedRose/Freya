{ pkgs, python, worker }:
pkgs.rustPlatform.buildRustPackage {
  pname = "freya-node";
  version = "0.1.0";
  src = pkgs.lib.cleanSource ../native/freya_node;
  cargoLock.lockFile = ../native/freya_node/Cargo.lock;
  nativeBuildInputs = [ pkgs.makeWrapper ];
  doCheck = false; # Operator validation only.
  postInstall = ''
    wrapProgram $out/bin/freya-node \
      --set FREYA_NODE_PYTHON ${python}/bin/python \
      --set FREYA_NODE_WORKER ${worker}
    mkdir -p $out/share/licenses/freya-node
    cp ${../LICENSE} $out/share/licenses/freya-node/LICENSE
  '';
  meta = { mainProgram = "freya-node"; license = pkgs.lib.licenses.agpl3Only; };
}
