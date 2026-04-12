# Qt 5.15.2 Android host tools (qmake, androiddeployqt, …) via aqtinstall.
# Fixed-output derivation: may fetch from Qt mirrors during build (license: LGPL-2.1 / Qt terms).
{
  lib,
  stdenv,
  cacert,
  python3,
  fetchPypi,
}:

let
  aqtinstall = python3.pkgs.buildPythonPackage rec {
    pname = "aqtinstall";
    version = "3.1.18";
    pyproject = true;

    src = fetchPypi {
      inherit pname version;
      sha256 = "1qp9wxkyl4mfq35a81ij58p3wkrkj8fgxpq5wwijbpq1vn2wyv81";
    };

    # PyPI name is beautifulsoup4; aqt lists "bs4" which breaks Nix runtime-deps check.
    postPatch = ''
      substituteInPlace pyproject.toml --replace-fail '"bs4"' '"beautifulsoup4"'
    '';

    nativeBuildInputs = with python3.pkgs; [
      setuptools
      setuptools-scm
    ];

    propagatedBuildInputs = with python3.pkgs; [
      beautifulsoup4
      defusedxml
      humanize
      patch
      py7zr
      requests
      semantic-version
      texttable
    ];

    doCheck = false;
  };

  pythonAqt = python3.withPackages (_: [ aqtinstall ]);
in

stdenv.mkDerivation {
  name = "qt-5.15.2-android-linux";

  outputHashMode = "recursive";
  outputHashAlgo = "sha256";
  # nix-build once with lib.fakeHash, then pin the reported hash (Qt archives from upstream mirrors).
  outputHash = "sha256-rLz/ZMxOGw2feo0tUNSz7aPLVuVPAVEgIrcLwQcUDXw=";

  nativeBuildInputs = [
    pythonAqt
    cacert
  ];

  dontUnpack = true;

  buildPhase = ''
    runHook preBuild
    export SSL_CERT_FILE=${cacert}/etc/ssl/certs/ca-bundle.crt
    export HOME=$(mktemp -d)
    export PYTHONNOUSERSITE=1
    ${pythonAqt}/bin/python -m aqt install-qt linux android 5.15.2 android -O "$out"
    runHook postBuild
  '';

  installPhase = ":";

  dontStrip = true;
  dontPatchELF = true;
  dontFixup = true;

  meta = with lib; {
    description = "Qt 5.15.2 for Android (host tools), fetched with aqtinstall";
    license = with licenses; [
      lgpl21Plus
      mit
    ];
    platforms = platforms.linux;
  };
}
