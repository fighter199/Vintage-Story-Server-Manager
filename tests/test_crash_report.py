"""Tests for core.crash_report."""
import os
import time

from core import crash_report as cr


class TestSummarize:
    def test_last_exception_wins(self):
        lines = ["Loading world", "System.IO.IOException: old",
                 "  at Foo.Bar()",
                 "Unhandled exception. System.NullReferenceException: Object "
                 "reference not set to an instance of an object.",
                 "   at Vintagestory.Server.ServerMain.Tick()"]
        assert cr.summarize(lines) == ("System.NullReferenceException: Object "
                                       "reference not set to an instance of an object.")

    def test_exception_without_message(self):
        assert cr.summarize(["x", "System.OutOfMemoryException"]) == \
            "System.OutOfMemoryException"

    def test_fatal_hint_then_last_line(self):
        assert cr.summarize(["ok", "Failed to bind port 42420", "bye"]) == \
            "Failed to bind port 42420"
        assert cr.summarize(["a", "last words", "  "]) == "last words"
        assert cr.summarize([]) == ""


class TestFiles:
    def test_find_game_crash_logs_since(self, tmp_path):
        logs = tmp_path / "Logs"
        (logs / "sub").mkdir(parents=True)
        old = logs / "server-crash-old.txt"
        old.write_text("old")
        os.utime(old, (time.time() - 3600,) * 2)
        new = logs / "server-crash.txt"
        new.write_text("new")
        nested = logs / "sub" / "crash-1.txt"
        nested.write_text("n")
        (logs / "server-main.txt").write_text("main")
        found = cr.find_game_crash_logs([str(logs), "", str(tmp_path / "x")],
                                        since=time.time() - 60)
        assert sorted(os.path.basename(p) for p in found) == \
            ["crash-1.txt", "server-crash.txt"]

    def test_build_and_write(self, tmp_path):
        game = tmp_path / "server-crash.txt"
        game.write_text("Game says: boom")
        console = [f"line {i}" for i in range(500)] + \
            ["System.InvalidOperationException: bad state"]
        text = cr.build_report(exit_code=-532462766, started=1000.0,
                               ended=1000.0 + 3725, console=console,
                               players=["Alice", "Bob"], game_logs=[str(game)],
                               app_version="3.6", server_path="/srv/vs")
        assert "Exit code:   -532462766" in text
        assert "Uptime:      1h 2m 5s" in text
        assert "Online:      Alice, Bob" in text
        assert "Likely cause: System.InvalidOperationException: bad state" in text
        assert "Game says: boom" in text
        assert "line 201\n" in text and "line 200\n" not in text   # last 300 only
        path = cr.write_report(str(tmp_path), text)
        assert os.path.dirname(path) == cr.reports_dir(str(tmp_path))
        with open(path, encoding="utf-8") as f:
            assert f.read() == text

    def test_keeps_newest_reports(self, tmp_path):
        folder = cr.reports_dir(str(tmp_path))
        os.makedirs(folder)
        for i in range(cr.MAX_REPORTS + 3):
            p = os.path.join(folder, f"crash-2026010{i:02d}.txt")
            with open(p, "w") as f:
                f.write("x")
            os.utime(p, (1000 + i, 1000 + i))
        newest = cr.write_report(str(tmp_path), "new")
        left = os.listdir(folder)
        assert len(left) == cr.MAX_REPORTS and os.path.basename(newest) in left
        assert "crash-202601000.txt" not in left
