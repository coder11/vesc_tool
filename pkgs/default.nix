{
  pkgs,
  src,
  bldcSrc,
  fwBoards ? [ ],

  gcc-arm-embedded-7,
}:
let
  android-sdk-vesc-bundle = pkgs.callPackage ./android-sdk-vesc.nix { };
  qt-515-android = pkgs.callPackage ./qt-515-android { };
  vesc-tool-android-release = pkgs.callPackage ./vesc-tool-android-release {
    androidsdk = android-sdk-vesc-bundle.androidsdk;
    qt515Android = qt-515-android;
  };
  # Flake `packages` must be derivations; composeAndroidPackages returns an attrset.
  android-sdk-vesc = android-sdk-vesc-bundle.androidsdk;
in
rec {
  vesc-tool = pkgs.callPackage ./vesc-tool {
    inherit
      src
      bldcSrc
      fwBoards
      gcc-arm-embedded-7
      ;
    kind = "original";
  };
  vesc-tool-platinum = vesc-tool.override { kind = "platinum"; };
  vesc-tool-gold = vesc-tool.override { kind = "gold"; };
  vesc-tool-silver = vesc-tool.override { kind = "silver"; };
  vesc-tool-bronze = vesc-tool.override { kind = "bronze"; };
  vesc-tool-copper = vesc-tool.override { kind = "copper"; };
  vesc-tool-free = vesc-tool.override { kind = "free"; };

  bldc-fw = pkgs.callPackage ./vesc-tool/bldc-fw.nix {
    inherit fwBoards gcc-arm-embedded-7;
    src = bldcSrc;
  };

  inherit
    android-sdk-vesc
    qt-515-android
    vesc-tool-android-release
    ;
}
