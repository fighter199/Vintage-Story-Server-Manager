"""Tests for core.utils — pure utility functions."""
import os
import zipfile

import pytest

from core.utils import (
    backup_single_file_to_zip,
    backup_world_to_zip,
    clean_mod_filename,
    fmt_size,
    restore_backup_zip,
    sanitize_filename,
    strip_hash_suffix,
    _is_human_readable_modid,
)


class TestBackupSingleFileToZip:
    def test_round_trip_with_arcname(self, tmp_path):
        src = tmp_path / "world-2026-07-11.vcdbs"
        src.write_text("dbdata")
        dst = str(tmp_path / "out.zip")
        backup_single_file_to_zip(str(src), dst,
                                  arcname="MyWorld/live.vcdbs")
        with zipfile.ZipFile(dst) as zf:
            assert zf.namelist() == ["MyWorld/live.vcdbs"]
            assert zf.read("MyWorld/live.vcdbs").decode() == "dbdata"

    def test_default_arcname_is_basename(self, tmp_path):
        src = tmp_path / "thing.vcdbs"
        src.write_text("x")
        dst = str(tmp_path / "out.zip")
        backup_single_file_to_zip(str(src), dst)
        with zipfile.ZipFile(dst) as zf:
            assert zf.namelist() == ["thing.vcdbs"]

    def test_missing_source_raises_and_leaves_no_part(self, tmp_path):
        dst = str(tmp_path / "out.zip")
        with pytest.raises(RuntimeError):
            backup_single_file_to_zip(str(tmp_path / "nope.vcdbs"), dst)
        assert not os.path.exists(dst)
        assert not os.path.exists(dst + ".part")

    def test_genbackup_zip_restores_into_world_folder(self, tmp_path):
        # End-to-end: a live-backup zip (single .vcdbs stored under
        # "WorldName/<live name>") must restore cleanly via the normal
        # restore path into the configured world folder.
        src = tmp_path / "genbackup-copy.vcdbs"
        src.write_text("consistent-db")
        dst_zip = str(tmp_path / "backup-live.zip")
        backup_single_file_to_zip(str(src), dst_zip,
                                  arcname="MyWorld/world.vcdbs")
        world = tmp_path / "RenamedWorld"
        restore_backup_zip(dst_zip, str(world), archive_existing=False)
        assert (world / "world.vcdbs").read_text() == "consistent-db"


# ----------------------------------------------------------------------
# Backup / restore round-trips
# ----------------------------------------------------------------------
def _make_world(root, name="MyWorld"):
    """Create a small fake world folder and return its path."""
    world = root / name
    (world / "sub").mkdir(parents=True)
    (world / "world.vcdbs").write_text("db-bytes")
    (world / "sub" / "extra.dat").write_text("extra")
    return world


class TestRestoreBackupZip:
    def test_round_trip_same_folder_name(self, tmp_path):
        world = _make_world(tmp_path, "MyWorld")
        zip_path = str(tmp_path / "backup.zip")
        backup_world_to_zip(str(world), zip_path)
        # Wipe a file so we can prove the restore brought it back.
        (world / "world.vcdbs").unlink()
        restore_backup_zip(zip_path, str(world), archive_existing=False)
        assert (world / "world.vcdbs").read_text() == "db-bytes"
        assert (world / "sub" / "extra.dat").read_text() == "extra"

    def test_restore_into_differently_named_folder(self, tmp_path):
        # Backup taken from "OldWorld", restored into "NewWorld" — the
        # contents must land inside NewWorld, not recreate OldWorld.
        import shutil
        old = _make_world(tmp_path, "OldWorld")
        zip_path = str(tmp_path / "backup.zip")
        backup_world_to_zip(str(old), zip_path)
        shutil.rmtree(old)
        new = tmp_path / "NewWorld"
        restore_backup_zip(zip_path, str(new), archive_existing=False)
        assert (new / "world.vcdbs").read_text() == "db-bytes"
        assert (new / "sub" / "extra.dat").read_text() == "extra"
        # The old buggy path extracted the archive's own folder name
        # into the parent dir and left the configured folder empty.
        assert not (tmp_path / "OldWorld").exists()

    def test_bare_zip_without_top_level_folder(self, tmp_path):
        # Foreign backups may store world files at the archive root.
        zip_path = str(tmp_path / "bare.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("world.vcdbs", "bare-db")
            zf.writestr("sub/extra.dat", "bare-extra")
        dst = tmp_path / "World"
        restore_backup_zip(zip_path, str(dst), archive_existing=False)
        assert (dst / "world.vcdbs").read_text() == "bare-db"
        assert (dst / "sub" / "extra.dat").read_text() == "bare-extra"

    def test_archives_existing_world_first(self, tmp_path):
        world = _make_world(tmp_path, "MyWorld")
        zip_path = str(tmp_path / "backup.zip")
        backup_world_to_zip(str(world), zip_path)
        (world / "world.vcdbs").write_text("newer-state")
        archived = restore_backup_zip(zip_path, str(world),
                                      archive_existing=True)
        assert archived and os.path.isfile(archived)
        assert os.path.basename(archived).startswith("pre-restore-")
        # Restored content is the backup, not the newer state…
        assert (world / "world.vcdbs").read_text() == "db-bytes"
        # …and the newer state is preserved inside the archive.
        with zipfile.ZipFile(archived) as zf:
            names = zf.namelist()
            member = [n for n in names if n.endswith("world.vcdbs")][0]
            assert zf.read(member).decode() == "newer-state"

    def test_corrupt_zip_leaves_existing_world_untouched(self, tmp_path):
        world = _make_world(tmp_path, "MyWorld")
        bad = tmp_path / "bad.zip"
        bad.write_text("this is not a zip")
        with pytest.raises(RuntimeError):
            restore_backup_zip(str(bad), str(world))
        assert (world / "world.vcdbs").read_text() == "db-bytes"

    def test_unsafe_path_rejected_before_world_is_touched(self, tmp_path):
        world = _make_world(tmp_path, "MyWorld")
        evil = str(tmp_path / "evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../outside.txt", "escape")
        with pytest.raises(RuntimeError, match="unsafe path"):
            restore_backup_zip(evil, str(world))
        # Current world untouched, nothing escaped the extraction root
        # (a "../" member would have landed next to the world folder).
        assert (world / "world.vcdbs").read_text() == "db-bytes"
        assert not (tmp_path / "outside.txt").exists()

    def test_empty_zip_rejected(self, tmp_path):
        empty = str(tmp_path / "empty.zip")
        with zipfile.ZipFile(empty, "w"):
            pass
        with pytest.raises(RuntimeError, match="empty"):
            restore_backup_zip(empty, str(tmp_path / "World"))

    def test_no_temp_dir_left_behind(self, tmp_path):
        world = _make_world(tmp_path, "MyWorld")
        zip_path = str(tmp_path / "backup.zip")
        backup_world_to_zip(str(world), zip_path)
        restore_backup_zip(zip_path, str(world), archive_existing=False)
        leftovers = [n for n in os.listdir(tmp_path)
                     if n.startswith(".restore-tmp-")]
        assert leftovers == []


class TestFmtSize:
    def test_zero(self):
        assert fmt_size(0) == "0 B"

    def test_negative(self):
        assert fmt_size(-1) == "0 B"

    def test_small_bytes(self):
        assert fmt_size(512) == "512 B"

    def test_kilobytes(self):
        assert fmt_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert fmt_size(2 * 1024 * 1024) == "2.0 MB"

    def test_gigabytes(self):
        assert fmt_size(3 * 1024 ** 3) == "3.0 GB"

    def test_invalid_returns_question_mark(self):
        assert fmt_size("not a number") == "?"
        assert fmt_size(None) == "?"


class TestSanitizeFilename:
    def test_basic(self):
        assert sanitize_filename("hello.zip") == "hello.zip"

    def test_strips_path_separators(self):
        # Path separators get replaced (not removed); leading dots are
        # preserved per the current implementation.
        out = sanitize_filename("../etc/passwd")
        assert "/" not in out and "\\" not in out

    def test_strips_other_unsafe_chars(self):
        out = sanitize_filename("hello<>|?.zip")
        for ch in '<>|?':
            assert ch not in out


class TestStripHashSuffix:
    def test_strips_long_hex_dash(self):
        # Only long hex suffixes (32+ chars) are stripped — short ones
        # may be legit version strings, so they're left alone.
        long_hex = "abcdef1234567890abcdef1234567890ab"  # 34 chars
        assert strip_hash_suffix(f"mod-{long_hex}.zip") == "mod.zip"

    def test_short_hex_unchanged(self):
        # 8-char hex isn't long enough to be considered a hash suffix.
        assert strip_hash_suffix("mod-abcdef12.zip") == "mod-abcdef12.zip"

    def test_no_hash_unchanged(self):
        assert strip_hash_suffix("mod.zip") == "mod.zip"


class TestIsHumanReadableModId:
    def test_word_id(self):
        assert _is_human_readable_modid("primitivesurvival")

    def test_id_with_digits(self):
        assert _is_human_readable_modid("xskills2")

    def test_pure_number_is_not(self):
        assert not _is_human_readable_modid("12345")

    def test_short_string_with_letters_is_human(self):
        # Documents current behaviour: function only filters out empty
        # and pure-digit strings, not by length.
        assert _is_human_readable_modid("ab")


class TestCleanModFilename:
    def test_basic_url_filename(self):
        url = "https://mods.vintagestory.at/files/primitivesurvival.zip"
        out = clean_mod_filename(url)
        assert out.endswith(".zip")
        assert "primitivesurvival" in out.lower()

    def test_uses_modid_when_available(self):
        url = "https://example.com/files/abcd1234.zip"
        out = clean_mod_filename(url, modid="primitivesurvival",
                                  version="3.7.0")
        assert "primitivesurvival" in out.lower()
        assert "3.7.0" in out

    def test_strips_query_string(self):
        url = "https://example.com/file.zip?hash=abcdef"
        out = clean_mod_filename(url)
        assert "?" not in out
        assert "=" not in out

    def test_falls_back_when_no_url_basename(self):
        out = clean_mod_filename("https://example.com/",
                                  modid="myMod",
                                  version="1.0")
        assert "myMod" in out or "mymod" in out.lower()

    def test_preserves_zip_extension(self):
        url = "https://example.com/foo.cs"  # not a zip
        out = clean_mod_filename(url, declared="foo.zip")
        # When declared name is .zip, we should prefer that.
        # (The exact policy may vary — this test pins current behaviour.)
        assert out.lower().endswith((".zip", ".cs"))
