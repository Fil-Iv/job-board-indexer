"""Platform display names (jobindex/platform_names.py) stay in sync with
coverage/platforms.yaml and cover every connector."""

from jobindex.connectors.registry import default_registry
from jobindex.platform_names import build_names, platform_name, platform_names


def test_names_match_platforms_yaml() -> None:
    assert platform_names() == build_names(), "run `make platform-names`"


def test_every_connector_has_a_name() -> None:
    missing = sorted(set(default_registry().platforms) - set(platform_names()))
    assert not missing


def test_lookup() -> None:
    assert platform_name("wpjobmanager") == "WP Job Manager"
    assert platform_name("successfactors") == "SAP SuccessFactors"
    assert platform_name("jobboardsoftware") == "jobboardsoftware.com"
    assert platform_name("unknown_key") == "unknown_key"
    assert platform_name(None) == ""
