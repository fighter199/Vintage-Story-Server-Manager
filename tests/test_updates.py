"""Tests for core.updates — version comparison and release parsing."""
import pytest

from core import updates
from core.updates import is_newer, parse_version, release_info


class TestVersions:
    def test_parse(self):
        assert parse_version("v3.4") == (3, 4)
        assert parse_version("3.4.1") == (3, 4, 1)
        assert parse_version("VSSM 3.10-beta") == (3, 10)
        assert parse_version("3.4.0") == (3, 4)
        assert parse_version("") == () and parse_version("latest") == ()

    def test_newer(self):
        assert is_newer("v3.5", "3.4")
        assert is_newer("v3.10", "3.9")            # numeric, not text
        assert is_newer("3.4.1", "3.4")
        assert not is_newer("v3.4", "3.4")
        assert not is_newer("v3.4.0", "3.4")
        assert not is_newer("v3.3", "3.4")
        assert not is_newer("nightly", "3.4")


class TestRelease:
    def test_release_info(self):
        info = release_info({"tag_name": "v3.5", "name": "VSSM 3.5",
                             "html_url": "https://example/rel", "body": "notes"})
        assert info == {"version": "3.5", "tag": "v3.5", "name": "VSSM 3.5",
                        "url": "https://example/rel", "notes": "notes"}

    def test_missing_fields_fall_back(self):
        info = release_info({"tag_name": "v3.5"})
        assert info["url"] == updates.RELEASES_URL and info["name"] == "v3.5"

    def test_network_errors_become_update_check_error(self):
        import urllib.error
        import urllib.request
        original = urllib.request.urlopen

        def fail(*_a, **_k):
            raise urllib.error.URLError("offline")
        urllib.request.urlopen = fail
        try:
            with pytest.raises(updates.UpdateCheckError, match="reach GitHub"):
                updates.fetch_latest_release(timeout=1)
        finally:
            urllib.request.urlopen = original
