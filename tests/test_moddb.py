"""Tests for mods.moddb._sanitize_url — the URL fixup that handles
ModDB's habit of returning download URLs with literal spaces in the
?dl= query parameter."""
from mods.moddb import _sanitize_url


class TestSanitizeUrl:
    def test_spaces_in_query_become_percent20(self):
        # Pinned regression for the user-reported "Auto Map Markers"
        # download failure. ModDB CDN returns the friendly filename
        # in the ?dl= param with literal spaces.
        url = ("https://moddbcdn.vintagestory.at/Auto+Map+Markers+5.0_"
               "420730ddb4c827f41e4d2b257e99a351.zip"
               "?dl=Auto Map Markers 5.0.1 - Vintage Story 1.22.zip")
        out = _sanitize_url(url)
        # The query value's spaces must be percent-encoded.
        assert "?dl=Auto%20Map%20Markers" in out
        assert " " not in out

    def test_already_encoded_not_double_encoded(self):
        url = "https://example.com/path%20with%20spaces?key=val%2Bplus"
        # Should pass through untouched — `%20` shouldn't become `%2520`.
        assert _sanitize_url(url) == url

    def test_clean_url_unchanged(self):
        url = "https://mods.vintagestory.at/foo.zip?dl=foo.zip"
        assert _sanitize_url(url) == url

    def test_empty_string(self):
        assert _sanitize_url("") == ""

    def test_unicode_in_query(self):
        # If a URL has unicode characters they should be percent-encoded
        # in UTF-8 form. (urllib.parse.quote handles this for us.)
        url = "https://example.com/path?name=résumé"
        out = _sanitize_url(url)
        assert " " not in out
        # The é should be encoded as %C3%A9.
        assert "%C3%A9" in out

    def test_plus_in_path_preserved(self):
        # ModDB sometimes uses '+' in path segments as a literal char,
        # not as a space stand-in. We keep it as-is in the path.
        url = ("https://moddbcdn.vintagestory.at/Auto+Map+Markers+5.0.zip"
               "?dl=clean.zip")
        out = _sanitize_url(url)
        assert "Auto+Map+Markers+5.0.zip" in out

    def test_preserves_scheme_and_host(self):
        url = "https://moddbcdn.vintagestory.at/foo bar.zip"
        out = _sanitize_url(url)
        assert out.startswith("https://moddbcdn.vintagestory.at/")

    def test_malformed_does_not_crash(self):
        # urlsplit accepts almost anything; just make sure we don't
        # raise on weird inputs.
        for bad in (None,):
            try:
                _sanitize_url(bad)
            except (TypeError, AttributeError):
                pass  # acceptable for None
