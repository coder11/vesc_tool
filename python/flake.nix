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
          # Stdlib tkinter + _tkinter; system/uv Pythons often omit it. Matplotlib TkAgg needs this.
          pythonWithTk = pkgs.python313.withPackages (ps: [ ps.tkinter ]);
          # PyPI wheels (numpy, matplotlib, …) link libstdc++; NixOS has no default FHS path.
          # Tk (Tcl/Tk) so matplotlib's TkAgg backend can dlopen libtk; avoids Agg + no window.
          linuxPyPiLdPath = pkgs.lib.optionalString pkgs.stdenv.isLinux ''
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
              pkgs.stdenv.cc.cc.lib
              pkgs.tk
            ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
            export MPLBACKEND=TkAgg
          '';
          pyShell = pkgs.mkShell {
            packages = [ pkgs.uv pythonWithTk ];
            shellHook = linuxPyPiLdPath + ''
              export UV_PYTHON="${pythonWithTk}/bin/python3"
              # Nix puts _tkinter in the wrapped interpreter's site-packages; uv's venv hides it unless
              # include-system-site-packages is true. Recreate .venv when it's missing or isolated.
              # Run `nix develop` from this directory (same folder as pyproject.toml), not via `cd` into the flake store path.
              if [[ -f pyproject.toml ]] && { [[ ! -d .venv ]] || ! grep -q '^include-system-site-packages = true' .venv/pyvenv.cfg 2>/dev/null; }; then
                echo "vesc-py dev shell: creating .venv with --system-site-packages (tkinter for matplotlib)."
                rm -rf .venv
                uv venv --system-site-packages
                uv sync
              fi
            '';
          };
        in {
          default = pyShell;
          python = pyShell;
        }
      );
    };
}
