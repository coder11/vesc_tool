import pytest

from vesc_py.config_paths import find_appconf_xml


def test_find_parameters_appconf_lists_available_on_miss() -> None:
    with pytest.raises(FileNotFoundError, match="Available versions"):
        find_appconf_xml(99, 99)


def test_find_known_version() -> None:
    path = find_appconf_xml(6, 6)
    assert path.name == "parameters_appconf.xml"
    assert path.exists()
