{
  description = "Packages VESC Tool into a flake.";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-25.05";
    # gcc-arm-embedded-7 has been removed from nixpkgs since 25.05 since it's
    # old and unmaintained. However, this project still uses that version, so we
    # include an older version of nixpkgs to access it.
    nixpkgsOld.url = "github:nixos/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
    treefmt-nix.url = "github:numtide/treefmt-nix";
    bldcSrc = {
      url = "github:vedderb/bldc/release_6_05";
      flake = false;
    };
  };

  # TODO: Add support for building on/for other systems.
  outputs =
    {
      self,
      nixpkgs,
      nixpkgsOld,
      flake-utils,
      treefmt-nix,
      bldcSrc,
    }@inputs:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          # Google Android SDK packages are unfree; android-sdk-vesc also sets licenseAccepted via override.
          config.allowUnfree = true;
        };
        treefmtEval = treefmt-nix.lib.evalModule pkgs ./treefmt.nix;
        selfPkgs = import ./pkgs {
          inherit pkgs bldcSrc;
          src = self;
          gcc-arm-embedded-7 = nixpkgsOld.legacyPackages.${system}.gcc-arm-embedded-7;
        };
      in
      {
        packages = selfPkgs // {
          default = selfPkgs.vesc-tool;
          # Same SDK/NDK bundle as used by vesc-tool-android-release (androidenv).
          vesc-android-sdk = selfPkgs.android-sdk-vesc;
          # Qt 5.15.2 Android host tools (fixed-output / aqtinstall); qmake at …/5.15.2/android/bin.
          qt-515-android = selfPkgs.qt-515-android;
        };

        apps = {
          android-release = {
            type = "app";
            program = "${selfPkgs.vesc-tool-android-release}/bin/vesc-tool-android-release";
          };
        };

        devShells.default = pkgs.mkShell {
          inputsFrom = [ selfPkgs.vesc-tool ];
          packages = [
            selfPkgs.android-sdk-vesc
            selfPkgs.qt-515-android
            selfPkgs.vesc-tool-android-release
          ];
        };

        # For `nix fmt`
        formatter = treefmtEval.config.build.wrapper;

        checks = {
          # For `nix flake check`
          formatting = treefmtEval.config.build.check self;
        };
      }
    )
    // {
      overlays.default = (nixpkgs.lib.makeOverridable (import ./overlay.nix)) {
        inherit bldcSrc nixpkgsOld;
        src = self;
      };
      # For development in the nix repl
      inherit self;
    };
}
