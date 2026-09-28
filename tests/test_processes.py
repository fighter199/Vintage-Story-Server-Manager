"""Tests for core.processes — spotting servers VSSM didn't start."""
import os

from core.processes import (_data_path_from_cmdline, describe,
                            find_external_servers)

DATA = os.path.abspath("/srv/VintagestoryData")
WORLD = os.path.join(DATA, "Saves")


def listing(*rows):
    return lambda: list(rows)


class TestFindExternalServers:
    def test_default_data_path_server_matches(self):
        found = find_external_servers(
            WORLD, processes=listing((101, "/opt/vs/VintagestoryServer", True),
                                     (102, "/usr/bin/python3 VSSM.py", True)),
            default_data=DATA)
        assert found == [{"pid": 101, "cmdline": "/opt/vs/VintagestoryServer",
                          "certain": True}]

    def test_own_server_excluded(self):
        found = find_external_servers(
            WORLD, own_pids=[101],
            processes=listing((101, "VintagestoryServer.exe", True)),
            default_data=DATA)
        assert found == []

    def test_dotnet_hosted_server(self):
        found = find_external_servers(
            WORLD, processes=listing((7, "dotnet /opt/vs/VintagestoryServer.dll", True)),
            default_data=DATA)
        assert [s["pid"] for s in found] == [7]

    def test_other_data_path_ignored(self):
        other = os.path.abspath("/srv/OtherServerData")
        found = find_external_servers(
            WORLD, processes=listing(
                (5, f'VintagestoryServer --dataPath "{other}"', True),
                (6, f"VintagestoryServer --dataPath={other}", True),
                (8, f"VintagestoryServer --dataPath {DATA}", True)),
            default_data=os.path.abspath("/elsewhere"))
        assert [s["pid"] for s in found] == [8]

    def test_name_only_listing_is_uncertain(self):
        found = find_external_servers(
            WORLD, processes=listing((9, "VintagestoryServer.exe", False)),
            default_data=DATA)
        assert found == [{"pid": 9, "cmdline": "VintagestoryServer.exe",
                          "certain": False}]

    def test_nothing_running(self):
        assert find_external_servers(WORLD, processes=listing(),
                                     default_data=DATA) == []


class TestHelpers:
    def test_data_path_parsing(self):
        assert _data_path_from_cmdline("VintagestoryServer --dataPath /a/b") == "/a/b"
        assert _data_path_from_cmdline("x --datapath=/c") == "/c"
        assert _data_path_from_cmdline("VintagestoryServer --port 42420") is None

    def test_describe_truncates(self):
        servers = [{"pid": i, "cmdline": "x" * 200, "certain": True} for i in range(7)]
        text = describe(servers)
        assert text.count("\n") == 5 and "and 2 more" in text
        assert "…" in text
