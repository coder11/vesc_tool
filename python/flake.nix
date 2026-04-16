{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in {
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python312.withPackages (ps: [
            ps.pydantic
            ps.pyserial
            ps.matplotlib
            ps.numpy
            ps.pytest
            ps.mypy
          ]);
        in {
          python = pkgs.mkShell {
            packages = [ python pkgs.uv ];
            shellHook = ''
              export PYTHONPATH="$PWD/src:''${PYTHONPATH:-}"
            '';
          };
        }
      );
    };
}
