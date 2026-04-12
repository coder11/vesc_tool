# Android SDK + NDK matching build_android (API 33 deploy, NDK 23.1.7779620, min API 23).
# Requires config.allowUnfree when importing nixpkgs (Google SDK terms).
{ androidenv }:

(androidenv.composeAndroidPackages.override { licenseAccepted = true; }) {
  platformVersions = [ "33" ];
  buildToolsVersions = [ "33.0.2" ];
  includeNDK = true;
  ndkVersions = [ "23.1.7779620" ];
  includeEmulator = false;
  includeSystemImages = false;
  includeCmake = false;
}
