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
          # PyPI wheels (numpy, matplotlib, …) link libstdc++; NixOS has no default FHS path.
          linuxPyPiLdPath = pkgs.lib.optionalString pkgs.stdenv.isLinux ''
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
          '';
          pyShell = pkgs.mkShell {
            packages = [ pkgs.uv ];
            shellHook = linuxPyPiLdPath;
          };
        in {
          default = pyShell;
          python = pyShell;
        }
      );
    };
}
