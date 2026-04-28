"""
probe_stdin.py — Diagnose why VS server isn't accepting commands from VSSM.

This is a self-contained probe that launches VintagestoryServer.exe the
same way VSSM does, sends a few harmless commands, and reports exactly
what the server does (or doesn't do) in response. Run from a terminal
on the same Windows box where VSSM lives.

Usage (from anywhere):
    python probe_stdin.py "C:\\path\\to\\VintagestoryServer.exe"

What it tells you:
    - whether the server opens its own console window when launched this way
    - whether it acknowledges a /list clients command
    - whether it acknowledges /stop
    - what its stdout actually emits (writes a probe.log next to this script)

Stops the server on its own at the end (or kills it if /stop is ignored).
"""
import os, subprocess, sys, time, threading, queue


def main():
    if len(sys.argv) < 2:
        print("Usage: python probe_stdin.py <path-to-VintagestoryServer.exe>")
        return 2
    exe = sys.argv[1]
    if not os.path.isfile(exe):
        print(f"ERROR: not a file: {exe}")
        return 2

    server_dir = os.path.dirname(exe)
    here = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(here, "probe.log")
    log = open(log_path, "w", encoding="utf-8")

    def say(msg):
        print(msg)
        log.write(msg + "\n")
        log.flush()

    say("=" * 70)
    say(f"probe_stdin.py — running on {sys.platform}")
    say(f"Python: {sys.version.split()[0]}")
    say(f"Server: {exe}")
    say(f"CWD:    {server_dir}")
    say("=" * 70)

    # Launch with the SAME flags VSSM currently uses.
    popen_kwargs = dict(
        cwd=server_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    if sys.platform.startswith("win"):
        # Match the patched VSSM behaviour.
        popen_kwargs["creationflags"] = 0x00000200 | 0x08000000

    say("Launching server …")
    t_launch = time.time()
    proc = subprocess.Popen([exe], **popen_kwargs)
    say(f"PID = {proc.pid}")

    # Reader thread, drains stdout to a queue
    q = queue.Queue()
    def reader():
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                q.put(line)
        except Exception as e:
            q.put(f"<reader exited: {e}>".encode())
    threading.Thread(target=reader, daemon=True).start()

    def drain(timeout=1.0, label=""):
        deadline = time.time() + timeout
        n = 0
        while time.time() < deadline:
            try:
                line = q.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(line, bytes):
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    text = repr(line)
            else:
                text = str(line).rstrip()
            log.write(f"[stdout] {text}\n")
            log.flush()
            n += 1
        if label:
            say(f"  {label}: {n} stdout lines drained over {timeout}s")
        return n

    # Wait for the server to come up.
    say("\nWaiting up to 90s for server to finish startup …")
    saw_running = False
    deadline = time.time() + 90
    while time.time() < deadline and proc.poll() is None:
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if isinstance(line, bytes):
            text = line.decode("utf-8", errors="replace").rstrip()
        else:
            text = str(line).rstrip()
        log.write(f"[stdout] {text}\n")
        if "Dedicated Server now running on Port" in text:
            saw_running = True
            say(f"  ✓ Server is up after {time.time()-t_launch:.1f}s")
            break
    log.flush()

    if not saw_running:
        say("  ✗ Server didn't reach 'Dedicated Server now running' within 90s")
        say("    (this is fine if your world is large; check probe.log)")

    say("\nGiving the server 5s to settle …")
    drain(5.0, "settle")

    # ----- Test 1: /list clients -------------------------------------
    say("\n--- Test 1: '/list clients' ---")
    payload = b"/list clients\n"
    say(f"Writing {len(payload)} bytes to stdin: {payload!r}")
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
        say("  write+flush OK")
    except Exception as e:
        say(f"  WRITE FAILED: {e}")

    n = drain(3.0, "after /list clients")
    if n == 0:
        say("  ✗ NO server response within 3s — server isn't reading our stdin.")
    else:
        say("  ✓ Server emitted output after /list clients (check probe.log)")

    # ----- Test 2: /list clients with CRLF ---------------------------
    say("\n--- Test 2: '/list clients' with \\r\\n line ending ---")
    payload = b"/list clients\r\n"
    say(f"Writing {len(payload)} bytes: {payload!r}")
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
        say("  write+flush OK")
    except Exception as e:
        say(f"  WRITE FAILED: {e}")
    n = drain(3.0, "after CRLF /list clients")

    # ----- Test 3: /stop ---------------------------------------------
    say("\n--- Test 3: '/stop' ---")
    payload = b"/stop\n"
    say(f"Writing {len(payload)} bytes: {payload!r}")
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
        say("  write+flush OK")
    except Exception as e:
        say(f"  WRITE FAILED: {e}")

    say("Waiting up to 30s for server to exit …")
    t0 = time.time()
    while time.time() - t0 < 30:
        if proc.poll() is not None:
            say(f"  ✓ Server exited after {time.time()-t0:.1f}s with code {proc.returncode}")
            drain(0.5, "post-exit")
            break
        drain(0.5, "")
    else:
        say("  ✗ Server still running after /stop — terminating it forcefully.")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    say("\n" + "=" * 70)
    say(f"Probe complete. Full output saved to:\n  {log_path}")
    say("=" * 70)
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
