{
  lib,
  writeShellApplication,
  jdk8_headless,
  git,
  zip,
  androidsdk,
  qt515Android,
}:

let
  # Matches androidenv cmdline-tools.nix: SDK root lives under libexec.
  sdkRoot = "${androidsdk}/libexec/android-sdk";
  qtKit = "${qt515Android}/5.15.2/android";
in
writeShellApplication {
  name = "vesc-tool-android-release";
  meta = with lib; {
    description = "Build VESC Tool Android release APKs (mobile + full) and vesc_tool_android.zip";
    longDescription = ''
      Run from the vesc_tool source tree (same layout as ./build_android).

      Android SDK/NDK (API 33, NDK 23.1.7779620), JDK 8, and Qt 5.15.2 for Android (aqtinstall) are
      provided by this flake. Override ''${QT_ANDROID_HOME} to use a different Qt kit.

      To use a host Android SDK instead of the Nix one, set ''${ANDROID_HOME} or ''${ANDROID_SDK_ROOT}
      before running (same as a manual build_android).
    '';
    license = with licenses; [
      lgpl21Plus
      mit
      unfree
    ];
    platforms = platforms.linux;
  };

  runtimeInputs = [
    jdk8_headless
    git
    zip
    androidsdk
  ];

  text = ''
    set -euo pipefail

    if [[ ! -f vesc_tool.pro ]]; then
      echo "error: run from the vesc_tool repository root (vesc_tool.pro not found in $PWD)" >&2
      exit 1
    fi

    if [[ -z "''${QT_ANDROID_HOME:-}" ]]; then
      QT_ANDROID_HOME="${qtKit}"
    fi
    if [[ ! -x "''${QT_ANDROID_HOME}/bin/qmake" ]]; then
      echo "error: ''${QT_ANDROID_HOME}/bin/qmake not found or not executable (set QT_ANDROID_HOME to override)" >&2
      exit 1
    fi
    export QT_ANDROID_HOME

    # Prefer host SDK if set; otherwise use Nix-provided SDK (android-sdk-vesc).
    ANDROID_SDK_ROOT="''${ANDROID_SDK_ROOT:-''${ANDROID_HOME:-${sdkRoot}}}"
    export ANDROID_HOME="$ANDROID_SDK_ROOT"
    export ANDROID_SDK_ROOT

    export ANDROID_NDK_HOST="''${ANDROID_NDK_HOST:-linux-x86_64}"
    export ANDROID_NDK_PLATFORM="''${ANDROID_NDK_PLATFORM:-android-23}"
    export ANDROID_NDK_TOOLCHAIN_VERSION="''${ANDROID_NDK_TOOLCHAIN_VERSION:-4.9}"

    NDK_VER="''${ANDROID_NDK_VERSION:-23.1.7779620}"
    if [[ -z "''${ANDROID_NDK_ROOT:-}" ]]; then
      export ANDROID_NDK_ROOT="$ANDROID_HOME/ndk/$NDK_VER"
    fi
    if [[ ! -d "$ANDROID_NDK_ROOT" ]]; then
      echo "error: ANDROID_NDK_ROOT ($ANDROID_NDK_ROOT) is missing. Install that NDK in the SDK or set ANDROID_NDK_ROOT." >&2
      exit 1
    fi

    export JAVA_HOME="${jdk8_headless.home}"
    export PATH="${androidsdk}/bin:$QT_ANDROID_HOME/bin:$PATH"

    rm -rf build/android/*

    build_one() {
      local config_line=$1
      local out_name=$2
      qmake -config release "$config_line" ANDROID_ABIS="arm64-v8a" -spec android-clang
      # GNU make's '%: %.o' built-in otherwise tries to link spurious targets (e.g. QCodeEditor) with the NDK.
      export MAKEFLAGS=-r
      make clean
      make -j"$(nproc)"
      make install INSTALL_ROOT=build/android/build
      androiddeployqt --gradle --no-gdbserver --output build/android/build \
        --input android-vesc_tool-deployment-settings.json --android-platform android-33
      mkdir -p build/android
      mv build/android/build/build/outputs/apk/debug/build-debug.apk "build/android/$out_name"
      rm -rf build/android/build
      rm -rf build/android/obj
      rm -f build/android/libvesc_tool*
    }

    build_one "CONFIG += release_android build_mobile" vesc_tool_mobile.apk
    build_one "CONFIG += release_android" vesc_tool_full.apk

    (
      cd build/android
      zip vesc_tool_android.zip vesc_tool_mobile.apk vesc_tool_full.apk
      rm -f vesc_tool_mobile.apk vesc_tool_full.apk
    )

    echo "Output: $PWD/build/android/vesc_tool_android.zip"
  '';
}
