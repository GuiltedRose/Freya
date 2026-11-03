{
  description = "Freya — Solana spot trading, research and finalized on-chain indexing";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/6f06d204d8f2d8761d9da78778745974a3216381";
  inputs.agave-source = {
    url = "github:anza-xyz/agave/825efd18292aff6ffcf9daa0f7612f21b3531a72";
    flake = false;
  };
  inputs.rust-overlay.url = "github:oxalica/rust-overlay";
  inputs.rust-overlay.inputs.nixpkgs.follows = "nixpkgs";
  outputs = { self, nixpkgs, agave-source, rust-overlay }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      environments = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          wheels = builtins.fromJSON (builtins.readFile ./nix/solders-wheels.json);
          python = pkgs.python313.override {
            packageOverrides = final: prev: {
              jsonalias = final.buildPythonPackage {
                pname = "jsonalias";
                version = "0.1.1";
                format = "wheel";
                src = pkgs.fetchurl {
                  url = "https://files.pythonhosted.org/packages/41/ed/05aebce69f78c104feff2ffcdd5a6f9d668a208aba3a8bf56e3750809fd8/jsonalias-0.1.1-py3-none-any.whl";
                  hash = "sha256-pW0oiOY5eBLGBhVlBOhh6OwA4YgAWvFJ8APHh9s9Pxg=";
                };
                pythonImportsCheck = [ "jsonalias" ];
              };
              solders = final.buildPythonPackage {
                pname = "solders";
                version = "0.29.0";
                format = "wheel";
                src = pkgs.fetchurl wheels.${system};
                nativeBuildInputs = pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.autoPatchelfHook ];
                buildInputs = pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.stdenv.cc.cc.lib ];
                dependencies = [ final.jsonalias final.typing-extensions ];
                pythonImportsCheck = [ "solders.pubkey" "solders.transaction" ];
              };
              websockets = final.buildPythonPackage {
                pname = "websockets";
                version = "15.0.1";
                pyproject = true;
                src = pkgs.fetchurl {
                  url = "https://files.pythonhosted.org/packages/21/e6/26d09fab466b7ca9c7737474c52be4f76a40301b08362eb2dbc19dcc16c1/websockets-15.0.1.tar.gz";
                  hash = "sha256-glRN4CB2uvugOM4FXuZBLWjaE6tH8MYMq4JzRt6Cje4=";
                };
                build-system = [ final.setuptools ];
                pythonImportsCheck = [ "websockets" ];
                doCheck = false;
              };
            };
          };
        in { inherit pkgs; pythonBase = python; python = python.withPackages (p: [ p.solders p.websockets p.zstandard ]); });
      nativeReady = builtins.pathExists ./native/solana_binary/Cargo.lock;
      nativePackage = system: import ./nix/native-decoder.nix {
        inherit (environments.${system}) pkgs;
        python = environments.${system}.pythonBase;
      };
      # Explicit source filter: no state, .env, private config, wallet files or venv.
      source = nixpkgs.lib.cleanSourceWith {
        src = ./.;
        filter = path: type:
          let rel = nixpkgs.lib.removePrefix (toString ./. + "/") (toString path);
          in if type == "directory" then builtins.elem rel [ "vendor" "tests" "tests/fixtures" "tests/fixtures/onchain" ]
          else nixpkgs.lib.hasSuffix ".py" rel
            || (nixpkgs.lib.hasPrefix "vendor/" rel && nixpkgs.lib.hasSuffix ".json" rel)
            || (nixpkgs.lib.hasPrefix "tests/fixtures/onchain/" rel && nixpkgs.lib.hasSuffix ".json" rel)
            || builtins.elem rel [ "meme_live_rules.json" "pump_watch_specs.json" "assets.json" "capital_policy.example.json" "challengers.example.json" "onchain_pipeline.example.json" "learning_pipeline.example.json" "selective_node.example.json" ];
      };
    in {
      packages = eachSystem (system:
        let inherit (environments.${system}) pkgs python;
        in {
          tmux = pkgs.tmux;
          default = pkgs.stdenvNoCC.mkDerivation {
            pname = "freya-paper";
            version = "0.1.0";
            src = source;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            dontBuild = true;
            installPhase = ''
              mkdir -p $out/share/freya $out/bin
              cp *.py meme_live_rules.json pump_watch_specs.json assets.json capital_policy.example.json challengers.example.json onchain_pipeline.example.json learning_pipeline.example.json selective_node.example.json $out/share/freya/
              cp -r vendor $out/share/freya/
              makeWrapper ${python}/bin/python $out/bin/freya-indexer \
                --add-flags "$out/share/freya/run_onchain_indexer.py"
              makeWrapper ${python}/bin/python $out/bin/freya-paper \
                --add-flags "$out/share/freya/run_paper.py"
              makeWrapper ${python}/bin/python $out/bin/freya-pipeline \
                --add-flags "$out/share/freya/run_bot_pipeline.py"
              makeWrapper ${python}/bin/python $out/bin/freya-live \
                --add-flags "$out/share/freya/run_meme_live.py"
              makeWrapper ${python}/bin/python $out/bin/freya-unified-learning \
                --add-flags "$out/share/freya/run_unified_learning.py"
              makeWrapper ${python}/bin/python $out/bin/freya-network-budget \
                --add-flags "$out/share/freya/solana_network_budget.py"
              makeWrapper ${python}/bin/python $out/bin/freya-observer \
                --add-flags "$out/share/freya/solana_observer.py"
              makeWrapper ${python}/bin/python $out/bin/freya-audit-ingestion \
                --add-flags "$out/share/freya/audit_ingestion.py"
              makeWrapper ${python}/bin/python $out/bin/freya-rpc-diagnostics \
                --add-flags "$out/share/freya/diagnose_mainnet_rpc.py"
              makeWrapper ${python}/bin/python $out/bin/freya-rpc-evidence \
                --add-flags "$out/share/freya/rpc_run_evidence.py"
              makeWrapper ${python}/bin/python $out/bin/freya-health \
                --add-flags "$out/share/freya/runtime_health.py"
              makeWrapper ${python}/bin/python $out/bin/freya-challengers \
                --add-flags "$out/share/freya/run_challengers.py"

              # Preserve existing NixOS modules and operator commands during rename.
              ln -s freya $out/share/crypto-bot
              for program in $out/bin/freya-*; do
                name="$(basename "$program")"
                ln -s "$name" "$out/bin/crypto-bot-''${name#freya-}"
              done
            '';
          };
         } // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
          agave-node = let
            nodePkgs = import nixpkgs { inherit system; overlays = [ rust-overlay.overlays.default ]; };
            toolchain = nodePkgs.rust-bin.stable."1.97.1".minimal;
          in import ./nix/agave-package.nix {
            pkgs = nodePkgs;
            src = agave-source;
            version = "4.3.0";
            rustPlatform = nodePkgs.makeRustPlatform { cargo = toolchain; rustc = toolchain; };
          };
          native-peer = let
            nodePkgs = import nixpkgs { inherit system; overlays = [ rust-overlay.overlays.default ]; };
            toolchain = nodePkgs.rust-bin.stable."1.97.1".minimal;
          in import ./nix/native-peer.nix {
            pkgs = nodePkgs;
            src = agave-source;
            rustPlatform = nodePkgs.makeRustPlatform { cargo = toolchain; rustc = toolchain; };
          };
         } // pkgs.lib.optionalAttrs nativeReady {
          native-node = import ./nix/native-node.nix {
            inherit pkgs;
            peer = self.packages.${system}.native-peer or null;
            python = environments.${system}.pythonBase.withPackages (p: [ p.solders p.websockets p.zstandard self.packages.${system}.native-rpc ]);
            worker = "${self.packages.${system}.default}/share/freya/run_selective_node.py";
          };
          bundle = pkgs.symlinkJoin {
            name = "freya-with-selective-node";
            paths = [ self.packages.${system}.native-rpc-app self.packages.${system}.native-node ];
          };
          native-decoder = nativePackage system;
          native-rpc = import ./nix/native-decoder.nix { inherit pkgs; python = environments.${system}.pythonBase; rpc = true; };
          native-rpc-app = pkgs.symlinkJoin {
            name = "freya-native-rpc-app";
            paths = [ self.packages.${system}.default ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              for program in $out/bin/freya-*; do
                wrapProgram "$program" --prefix PYTHONPATH : "${self.packages.${system}.native-rpc}/${environments.${system}.pythonBase.sitePackages}"
              done
            '';
          };
          native-app = pkgs.symlinkJoin {
            name = "freya-native-app";
            paths = [ self.packages.${system}.default ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              for program in $out/bin/freya-*; do
                wrapProgram "$program" --prefix PYTHONPATH : "${nativePackage system}/${environments.${system}.pythonBase.sitePackages}"
              done
            '';
          };
        });
      apps = eachSystem (system: {
        default = self.apps.${system}.indexer;
        indexer = { type = "app"; program = "${self.packages.${system}.native-rpc-app}/bin/freya-indexer"; };
        observer = { type = "app"; program = "${self.packages.${system}.native-rpc-app}/bin/freya-observer"; };
        audit-ingestion = { type = "app"; program = "${self.packages.${system}.native-rpc-app}/bin/freya-audit-ingestion"; };
        node = { type = "app"; program = "${self.packages.${system}.native-node}/bin/freya-node"; };
        rpc-evidence = { type = "app"; program = "${self.packages.${system}.native-rpc-app}/bin/freya-rpc-evidence"; };
        rpc-diagnostics = { type = "app"; program = "${self.packages.${system}.native-rpc-app}/bin/freya-rpc-diagnostics"; };
        tmux = { type = "app"; program = "${environments.${system}.pkgs.tmux}/bin/tmux"; };
        paper = {
          type = "app";
          program = toString (environments.${system}.pkgs.writeShellScript "freya-paper-runner" ''
            exec ${self.packages.${system}.native-rpc-app}/bin/freya-pipeline --mode paper "$@"
          '');
        };
        paper-raw = { type = "app"; program = "${self.packages.${system}.native-rpc-app}/bin/freya-paper"; };
        live = {
          type = "app";
          program = toString (environments.${system}.pkgs.writeShellScript "freya-live-runner" ''
            exec ${self.packages.${system}.native-rpc-app}/bin/freya-pipeline --mode live "$@"
          '');
        };
        live-raw = { type = "app"; program = "${self.packages.${system}.native-rpc-app}/bin/freya-live"; };
        collect = {
          type = "app";
          program = toString (environments.${system}.pkgs.writeShellScript "freya-collection-runner" ''
            exec ${self.packages.${system}.native-rpc-app}/bin/freya-pipeline --mode indexer "$@"
          '');
        };
        data = {
          type = "app";
          program = toString (environments.${system}.pkgs.writeShellScript "freya-data-runner" ''
            exec ${self.packages.${system}.native-rpc-app}/bin/freya-pipeline --mode data "$@"
          '');
        };
      });
      devShells = eachSystem (system:
        let inherit (environments.${system}) pkgs python;
        in { default = pkgs.mkShell { packages = [ python pkgs.sqlite pkgs.tmux pkgs.zstd pkgs.rustc pkgs.cargo pkgs.rustfmt pkgs.clippy ] ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.nftables pkgs.iproute2 pkgs.iputils pkgs.procps ]; }; }
          // pkgs.lib.optionalAttrs nativeReady {
            native-rpc = pkgs.mkShell {
              packages = [ (environments.${system}.pythonBase.withPackages (p: [ p.solders p.websockets p.zstandard self.packages.${system}.native-rpc ])) pkgs.rustc pkgs.cargo pkgs.sqlite pkgs.zstd ]
                ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.nftables pkgs.iproute2 pkgs.iputils pkgs.procps pkgs.util-linux ];
            };
            native = pkgs.mkShell {
              packages = [ (environments.${system}.pythonBase.withPackages (p: [ p.solders p.websockets p.zstandard (nativePackage system) ])) pkgs.rustc pkgs.cargo pkgs.rustfmt pkgs.clippy ];
            };
          } // pkgs.lib.optionalAttrs (nativeReady && system == "x86_64-linux") {
            native-mainnet = pkgs.mkShell {
              inputsFrom = [ self.devShells.${system}.native-rpc ];
              packages = [
                self.packages.${system}.native-node
                self.packages.${system}.native-peer
                self.packages.${system}.native-rpc-app
                (pkgs.writeShellScriptBin "freya-mainnet-preflight" ''
                  exec ${environments.${system}.pythonBase}/bin/python ${./native_mainnet_preflight.py} "$@"
                '')
                pkgs.ethtool pkgs.jq pkgs.systemd
              ];
              # Entering the shell never starts a service or authorizes networking.
            };
          });
      checks = eachSystem (system:
        let inherit (environments.${system}) pkgs python;
        in { indexer = pkgs.runCommand "freya-indexer-tests" { nativeBuildInputs = [ python pkgs.zstd ]; } ''
          cp -r ${source} source
          chmod -R u+w source
          cd source
          python -m unittest discover -s tests -p test_onchain_indexer.py
          python -m unittest discover -s tests -p test_block_batch.py
          python -m unittest discover -s tests -p test_onchain_hour.py
          python -m unittest discover -s tests -p test_onchain_stream_candles.py
          python -m unittest discover -s tests -p test_selfhost_observer.py
          python -m unittest discover -s tests -p test_solana_observer.py
          python -m unittest discover -s tests -p test_pipeline_repair.py
          python -m unittest discover -s tests -p test_production_pipeline.py
          python -m unittest discover -s tests -p test_dynamic_capital.py
          python -m unittest discover -s tests -p test_challenger_management.py
          python -m unittest discover -s tests -p test_pipeline_startup.py
          python -m unittest discover -s tests -p test_pipeline_setup.py
          python -m unittest discover -s tests -p test_pipeline_console.py
          python -m unittest discover -s tests -p test_runtime_completion.py
          touch $out
        ''; } // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
          solana-network-lifecycle = import ./tests/nix/solana-network-lifecycle.nix { inherit pkgs; };
        });
      nixosModules.observer-workflows = { config, lib, pkgs, ... }: {
        imports = [ self.nixosModules.solana-budget-isolation
          (import ./nix/observer-workflows.nix {
            inherit config lib pkgs;
            package = self.packages.${pkgs.stdenv.hostPlatform.system}.native-rpc-app;
          }) ];
      };
      nixosModules.solana-budget-isolation = import ./nix/solana-network-isolation.nix;
      lib.mkAgavePackage = args: import ./nix/agave-package.nix args;
      nixosModules.selfhost-rpc = { config, lib, pkgs, ... }: import ./nix/selfhost-rpc.nix {
        inherit config lib pkgs;
        nodePackage = self.packages.${pkgs.stdenv.hostPlatform.system}.agave-node or pkgs.emptyDirectory;
      };
      nixosModules.native-node = { config, lib, pkgs, ... }: import ./nix/selective-node.nix {
        inherit config lib pkgs;
        package = self.packages.${pkgs.stdenv.hostPlatform.system}.native-node;
      };
      nixosModules.default = { config, lib, pkgs, ... }:
        import ./nix/module.nix {
          inherit config lib pkgs;
          package = self.packages.${pkgs.stdenv.hostPlatform.system}.native-rpc-app;
        };
    };
}
