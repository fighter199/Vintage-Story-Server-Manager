"""Tests for ui.theme — readable contrast and font sizing."""
from ui.theme import ColorRemap, Theme, contrast_ratio, font_sizes, palette

TEXT_COLOURS = ("AMBER", "AMBER_DIM", "AMBER_GLOW", "MUTED",
                "GREEN", "RED", "CYAN", "PURPLE")
BACKGROUNDS = ("BG_PANEL", "BG_INPUT", "BG_DARK", "BG_HEADER")


def _palette(preset):
    Theme.apply_preset(preset)
    try:
        return {k: getattr(Theme, k) for k in dir(Theme)
                if k.isupper() and isinstance(getattr(Theme, k), str)}
    finally:
        Theme.apply_preset("amber")


class TestContrast:
    def test_contrast_ratio_reference_values(self):
        assert round(contrast_ratio("#000000", "#ffffff"), 1) == 21.0
        assert round(contrast_ratio("#777777", "#ffffff"), 2) == 4.48
        assert contrast_ratio("#123456", "#123456") == 1.0

    def test_every_preset_meets_wcag_aa_for_text(self):
        for preset in ("amber", "green", "cyan", "dark"):
            pal = _palette(preset)
            for fg in TEXT_COLOURS:
                for bg in BACKGROUNDS:
                    ratio = contrast_ratio(pal[fg], pal[bg])
                    assert ratio >= 4.5, (preset, fg, bg, round(ratio, 2))

    def test_dim_text_stays_distinct_from_hints(self):
        # Secondary text and hint text shouldn't collapse into one grey.
        for preset in ("amber", "green", "cyan", "dark"):
            pal = _palette(preset)
            assert pal["AMBER_DIM"] != pal["MUTED"]
            assert contrast_ratio(pal["AMBER"], pal["BG_PANEL"]) > \
                contrast_ratio(pal["AMBER_DIM"], pal["BG_PANEL"])

    def test_reset_restores_amber(self):
        Theme.apply_preset("green")
        Theme.apply_preset("amber")
        assert Theme.AMBER_DIM == "#b57d00"


class TestFontSizes:
    def test_default_sizes_are_readable(self):
        sizes = font_sizes()
        assert sizes["F_SMALL"] == (9, False)
        assert sizes["F_HDR"] == (10, True)
        assert min(pt for pt, _ in sizes.values()) >= 9

    def test_scale_is_linear_not_squared(self):
        assert font_sizes(1.5)["F_NORMAL"][0] == 15
        assert font_sizes(2.0)["F_SMALL"][0] == 18

    def test_scale_is_clamped(self):
        assert font_sizes(10)["F_NORMAL"] == font_sizes(2.0)["F_NORMAL"]
        assert font_sizes(0.1)["F_SMALL"][0] >= 7

    def test_pixel_font_and_macos_boost(self):
        assert font_sizes(pixel_font=True)["F_NORMAL"][0] == 13
        assert font_sizes(aqua=True)["F_NORMAL"][0] == 13
        assert font_sizes(pixel_font=True, aqua=True)["F_NORMAL"][0] == 16


class TestColorRemap:
    def _remap(self, old_preset, new_preset, custom=None):
        try:
            Theme.apply_preset(old_preset)
            old = palette()
            Theme.apply_preset(new_preset)
            if custom:
                Theme.load_custom_colors(custom)
            return ColorRemap(old, palette())
        finally:
            Theme.apply_preset("amber")

    def test_maps_every_palette_colour(self):
        remap = self._remap("amber", "green")
        assert remap
        assert remap("#ffb000") == "#33ff33"            # AMBER
        assert remap("#FFB000") == "#33ff33"            # case-insensitive
        assert remap("#b57d00") == "#1e9d1e"            # AMBER_DIM
        assert remap("#332200") == "#0a3a0a"            # BORDER

    def test_other_colours_and_names_untouched(self):
        remap = self._remap("amber", "green")
        for value in ("#111111", "#ff4444", "", "SystemButtonFace", None, 3):
            assert remap(value) == value

    def test_same_preset_changes_nothing(self):
        remap = self._remap("green", "green")
        assert not remap
        assert remap("#33ff33") == "#33ff33"

    def test_shared_colour_resolved_by_widget_and_option(self):
        # amber's BG_SELECT and BG_BTN_AMBER_HOVER are both #2a1d00;
        # green's differ (#003300 / #002b00).
        remap = self._remap("amber", "green")
        assert remap("#2a1d00", "background") == "#003300"
        assert remap("#2a1d00", "selectbackground") == "#003300"
        assert remap("#2a1d00", "background", button=True) == "#002b00"

    def test_custom_palette_splits_header_and_button(self):
        remap = self._remap("amber", "custom",
                            {"BG_HEADER": "#101010", "BG_BTN_AMBER": "#202020"})
        assert remap("#1a1200", "background") == "#101010"
        assert remap("#1a1200", "background", button=True) == "#202020"

    def test_foreground_option_prefers_text_colour(self):
        # A custom palette where a text and a background key collide.
        remap = ColorRemap({"AMBER": "#123456", "BG_HEADER": "#123456"},
                           {"AMBER": "#ffffff", "BG_HEADER": "#000000"})
        assert remap("#123456", "foreground") == "#ffffff"
        assert remap("#123456", "-fg") == "#ffffff"
        assert remap("#123456", "background") == "#000000"
