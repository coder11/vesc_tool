"""Resolve firmware-version-specific config directories matching Utility::configLoad."""

from __future__ import annotations

from pathlib import Path

# res/config/ is relative to the vesc_tool repo root (parent of python/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_DIR = _REPO_ROOT / "res" / "config"


def _available_versions(config_dir: Path | None = None) -> list[tuple[int, int, Path]]:
    """Return (major, minor, dir_path) for all FW config directories."""
    base = config_dir or _CONFIG_DIR
    versions: list[tuple[int, int, Path]] = []
    if not base.is_dir():
        return versions

    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        # Directory name may contain _o_ separating multiple version aliases
        for segment in entry.name.split("_o_"):
            parts = segment.split(".")
            if len(parts) == 2:
                try:
                    major = int(parts[0])
                    minor = int(parts[1])
                    versions.append((major, minor, entry))
                except ValueError:
                    continue

    return versions


def find_appconf_xml(
    fw_major: int,
    fw_minor: int,
    config_dir: Path | None = None,
) -> Path:
    """Find the parameters_appconf.xml for a given firmware version.

    Raises FileNotFoundError with a list of available versions on miss.
    """
    for major, minor, dir_path in _available_versions(config_dir):
        if major == fw_major and minor == fw_minor:
            appconf = dir_path / "parameters_appconf.xml"
            if appconf.exists():
                return appconf
            raise FileNotFoundError(
                f"Config directory {dir_path} exists but parameters_appconf.xml is missing"
            )

    avail = _available_versions(config_dir)
    version_strs = sorted(f"{m}.{n:02d}" for m, n, _ in avail)
    raise FileNotFoundError(
        f"No config for firmware {fw_major}.{fw_minor:02d}. "
        f"Available versions: {', '.join(version_strs)}"
    )
