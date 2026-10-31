{
  description = "Original crypto bot - isolated paper experiment";

  inputs.nixpkgs.url =
    "github:NixOS/nixpkgs/6f06d204d8f2d8761d9da78778745974a3216381";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

      python = pkgs.python311.withPackages (ps: [
        ps.solders
        ps.websockets
      ]);
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = [ python pkgs.sqlite ];
      };

      apps.${system}.paper = {
        type = "app";
        program = "${pkgs.writeShellScript "baseline-paper" ''
          exec ${python}/bin/python ./run_paper.py "$@"
        ''}";
      };
    };
}
