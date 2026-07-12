"""Tests for backup.manager — reason-based filename families and
per-family retention pruning."""
import os
import time

from backup.manager import BackupManager, prefix_for_reason


class FakeHost:
    """Minimal synchronous stand-in for ServerManagerApp."""
    is_running = False

    def __init__(self, backup_dir, world="", max_backups=0,
                 max_start=0, max_stop=0, retention="count"):
        self._backup_dir = backup_dir
        self._world = world
        self._max = max_backups
        self._max_start = max_start
        self._max_stop = max_stop
        self._retention = retention
        self.console = []

    def append_console(self, text, tag="info"):
        self.console.append(text)

    def _notify(self, *args, **kwargs):
        pass

    def _send_internal_command(self, cmd):
        return True

    def after(self, ms, fn=None, *args):
        if fn:
            fn(*args)

    def get_world_folder(self):
        return self._world

    def get_backup_dir(self):
        return self._backup_dir

    def get_max_backups(self):
        return self._max

    def get_max_start_backups(self):
        return self._max_start

    def get_max_stop_backups(self):
        return self._max_stop

    def get_retention_mode(self):
        return self._retention

    def get_autosave_cmd_enabled(self):
        return False

    def get_server_backups_dir(self):
        return ""

    def _refresh_backup_list(self):
        pass


def _mk(dirpath, name, age_secs):
    """Create a small file with its mtime pushed `age_secs` into the
    past, so 'newest first' ordering is deterministic."""
    full = os.path.join(str(dirpath), name)
    with open(full, "w") as f:
        f.write("x")
    t = time.time() - age_secs
    os.utime(full, (t, t))
    return full


class TestPrefixForReason:
    def test_pre_start(self):
        assert prefix_for_reason("pre-start") == "startbackup-"

    def test_stop(self):
        assert prefix_for_reason("stop") == "stopbackup-"

    def test_manual_and_autosave_share_default(self):
        assert prefix_for_reason("manual") == "backup-"
        assert prefix_for_reason("autosave") == "backup-"

    def test_unknown_or_missing_reason_defaults(self):
        assert prefix_for_reason("") == "backup-"
        assert prefix_for_reason(None) == "backup-"

    def test_prefixes_are_disjoint(self):
        # startswith-based pruning relies on no family prefix being a
        # prefix of another family's filenames.
        assert not "startbackup-x.zip".startswith("backup-")
        assert not "stopbackup-x.zip".startswith("backup-")


class TestPruneFamilies:
    def test_families_pruned_independently(self, tmp_path):
        for i in range(4):
            _mk(tmp_path, f"backup-{i}.zip", age_secs=i * 60)
            _mk(tmp_path, f"startbackup-{i}.zip", age_secs=i * 60)
            _mk(tmp_path, f"stopbackup-{i}.zip", age_secs=i * 60)
        host = FakeHost(str(tmp_path), max_backups=3,
                        max_start=1, max_stop=2)
        BackupManager(host).prune_old_backups(announce=False)
        left = sorted(os.listdir(str(tmp_path)))
        assert [n for n in left if n.startswith("backup-")] == \
            ["backup-0.zip", "backup-1.zip", "backup-2.zip"]
        assert [n for n in left if n.startswith("startbackup-")] == \
            ["startbackup-0.zip"]
        assert [n for n in left if n.startswith("stopbackup-")] == \
            ["stopbackup-0.zip", "stopbackup-1.zip"]

    def test_zero_cap_keeps_all(self, tmp_path):
        for i in range(3):
            _mk(tmp_path, f"startbackup-{i}.zip", age_secs=i * 60)
        host = FakeHost(str(tmp_path), max_backups=0,
                        max_start=0, max_stop=0)
        BackupManager(host).prune_old_backups(announce=False)
        assert len(os.listdir(str(tmp_path))) == 3

    def test_newest_kept_oldest_deleted(self, tmp_path):
        _mk(tmp_path, "stopbackup-old.zip", age_secs=3600)
        _mk(tmp_path, "stopbackup-new.zip", age_secs=10)
        host = FakeHost(str(tmp_path), max_stop=1)
        BackupManager(host).prune_old_backups(announce=False)
        assert os.listdir(str(tmp_path)) == ["stopbackup-new.zip"]

    def test_start_stop_families_do_not_count_against_main(self, tmp_path):
        # One regular backup + one start backup: a main cap of 1 must
        # not treat the start backup as part of the main family.
        _mk(tmp_path, "backup-a.zip", age_secs=100)
        _mk(tmp_path, "startbackup-b.zip", age_secs=50)
        host = FakeHost(str(tmp_path), max_backups=1)
        BackupManager(host).prune_old_backups(announce=False)
        assert sorted(os.listdir(str(tmp_path))) == \
            ["backup-a.zip", "startbackup-b.zip"]

    def test_non_zip_files_untouched(self, tmp_path):
        _mk(tmp_path, "stopbackup-note.txt", age_secs=9999)
        _mk(tmp_path, "stopbackup-a.zip", age_secs=100)
        _mk(tmp_path, "stopbackup-b.zip", age_secs=50)
        host = FakeHost(str(tmp_path), max_stop=1)
        BackupManager(host).prune_old_backups(announce=False)
        left = sorted(os.listdir(str(tmp_path)))
        assert "stopbackup-note.txt" in left
        assert "stopbackup-b.zip" in left
        assert "stopbackup-a.zip" not in left

    def test_host_without_family_getters_keeps_all(self, tmp_path):
        # Older host objects (or tests) that don't implement the new
        # getters must behave as cap 0 = keep everything.
        for i in range(3):
            _mk(tmp_path, f"startbackup-{i}.zip", age_secs=i * 60)
        host = FakeHost(str(tmp_path))
        del FakeHost.get_max_start_backups  # simulate missing getter
        try:
            BackupManager(host).prune_old_backups(announce=False)
            assert len(os.listdir(str(tmp_path))) == 3
        finally:
            FakeHost.get_max_start_backups = lambda self: self._max_start
