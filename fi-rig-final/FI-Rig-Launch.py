#!/usr/bin/env python3
"""
FI-Rig-Launch.py
================
Double-click this file (or run: python FI-Rig-Launch.py)

What it does, automatically:
  1. Checks your Python version (needs 3.9+)
  2. Installs all required packages via pip (only on first run)
  3. Starts the FI Rig API server on http://localhost:8765
  4. Opens your browser to the web UI
  5. Shows a live log — press Ctrl+C to shut everything down cleanly
"""

import sys
import os
import subprocess
import time
import webbrowser
import platform
import textwrap

# ── Colour helpers (work on Windows 10+ and all terminals) ──────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
DIM    = "\033[2m"

def enable_windows_ansi():
    """Enable ANSI escape codes on Windows 10+."""
    if platform.system() == "Windows":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

def info(msg):  print(f"{CYAN}[fi-rig]{RESET} {msg}")
def ok(msg):    print(f"{GREEN}[  OK  ]{RESET} {msg}")
def warn(msg):  print(f"{YELLOW}[ WARN ]{RESET} {msg}")
def fail(msg):  print(f"{RED}[ FAIL ]{RESET} {msg}"); sys.exit(1)
def step(msg):  print(f"\n{BOLD}── {msg}{RESET}")

# ── Banner ───────────────────────────────────────────────────────────────────

BANNER = f"""
{CYAN}{BOLD}
  ███████╗██╗    ██████╗ ██╗ ██████╗
  ██╔════╝██║    ██╔══██╗██║██╔════╝
  █████╗  ██║    ██████╔╝██║██║  ███╗
  ██╔══╝  ██║    ██╔══██╗██║██║   ██║
  ██║     ██║    ██║  ██║██║╚██████╔╝
  ╚═╝     ╚═╝    ╚═╝  ╚═╝╚═╝ ╚═════╝
{RESET}{DIM}  ESP32-S3 Fault Injection Rig — v1.0.0{RESET}
"""

# ── Required packages ────────────────────────────────────────────────────────

REQUIRED_PACKAGES = [
    ("pyserial",            "pyserial>=3.5"),
    ("rich",                "rich>=13.0"),
    ("fastapi",             "fastapi>=0.110"),
    ("uvicorn",             "uvicorn[standard]>=0.29"),
    ("websockets",          "websockets>=12.0"),
    ("numpy",               "numpy>=1.24"),
    ("matplotlib",          "matplotlib>=3.7"),
    ("typer",               "typer>=0.12"),
    ("pydantic",            "pydantic>=2.0"),
    ("aiofiles",            "aiofiles>=23.0"),
]

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
HOST_PORT   = 8765
API_URL     = f"http://127.0.0.1:{HOST_PORT}"
BROWSER_URL = f"http://localhost:{HOST_PORT}"
LOG_DIR     = os.path.join(SCRIPT_DIR, "fi_logs")
WEB_DIR     = os.path.join(SCRIPT_DIR, "web")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Python version check
# ─────────────────────────────────────────────────────────────────────────────

def check_python():
    step("Checking Python version")
    major, minor = sys.version_info.major, sys.version_info.minor
    info(f"Python {major}.{minor}.{sys.version_info.micro} — {platform.system()} {platform.machine()}")
    if major < 3 or (major == 3 and minor < 9):
        fail(
            f"Python 3.9+ required, you have {major}.{minor}.\n"
            "       Download from https://python.org — check 'Add Python to PATH'"
        )
    ok(f"Python {major}.{minor} is compatible")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Install missing packages
# ─────────────────────────────────────────────────────────────────────────────

def install_packages():
    step("Checking / installing required packages")

    missing = []
    for import_name, pip_spec in REQUIRED_PACKAGES:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_spec)

    if not missing:
        ok("All packages already installed")
        return

    info(f"Installing {len(missing)} package(s): {', '.join(missing)}")
    info("This only happens once — please wait...")

    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"] + missing
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(result.stderr)
        fail(
            "pip install failed.\n"
            "       Try running manually:\n"
            f"       python -m pip install {' '.join(missing)}"
        )

    ok(f"Installed: {', '.join(missing)}")

    # Also install the fi_host package itself in editable mode
    pyproject = os.path.join(SCRIPT_DIR, "pyproject.toml")
    if os.path.exists(pyproject):
        info("Installing fi_host package...")
        r2 = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
            cwd=SCRIPT_DIR,
            capture_output=True, text=True,
        )
        if r2.returncode == 0:
            ok("fi_host package installed")
        else:
            # Not fatal — server can run from PYTHONPATH too
            warn("fi_host editable install skipped (will use PYTHONPATH)")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Start the API server
# ─────────────────────────────────────────────────────────────────────────────

def start_server():
    step("Starting FI Rig API server")

    os.makedirs(LOG_DIR, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"]    = SCRIPT_DIR
    env["FI_LOG_DIR"]    = LOG_DIR
    env["FI_WEB_ROOT"]   = WEB_DIR
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        sys.executable, "-m", "uvicorn",
        "fi_host.server:app",
        "--host", "127.0.0.1",
        "--port", str(HOST_PORT),
        "--log-level", "warning",   # suppress routine request logs
    ]

    info(f"Server: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        cwd        = SCRIPT_DIR,
        env        = env,
        stdout     = subprocess.PIPE,
        stderr     = subprocess.STDOUT,
        text       = True,
        bufsize     = 1,
    )
    return proc

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Wait for server to be ready
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_server(proc, retries=40, delay=0.3):
    import urllib.request
    import urllib.error

    info(f"Waiting for server at {API_URL} ...")
    for i in range(retries):
        # Check if process died early
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            fail(
                f"Server process exited unexpectedly (code {proc.returncode}).\n"
                f"Output:\n{output}"
            )
        try:
            with urllib.request.urlopen(f"{API_URL}/api/status", timeout=1) as r:
                if r.status < 500:
                    ok(f"Server ready after {(i+1)*delay:.1f}s")
                    return
        except Exception:
            pass
        time.sleep(delay)

    # Server didn't respond — still try to open browser
    warn("Server did not respond in time — opening browser anyway")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Open browser
# ─────────────────────────────────────────────────────────────────────────────

def open_browser():
    step("Opening browser")
    info(f"URL: {BROWSER_URL}")
    time.sleep(0.5)
    webbrowser.open(BROWSER_URL)
    ok("Browser launched")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Keep running, stream server logs
# ─────────────────────────────────────────────────────────────────────────────

def run_until_exit(proc):
    print(f"""
{GREEN}{BOLD}FI Rig is running!{RESET}

  Web UI : {BOLD}{BROWSER_URL}{RESET}
  API    : {BOLD}{API_URL}/docs{RESET}
  Logs   : {BOLD}{LOG_DIR}{RESET}

  {DIM}Connect your ESP32-S3, select the COM port in the UI, and click Connect.{RESET}

  {YELLOW}Press Ctrl+C to stop.{RESET}
──────────────────────────────────────────────
""")

    try:
        while True:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            # Only print non-empty, non-routine lines
            stripped = line.strip()
            if stripped and "GET /api/status" not in stripped:
                print(f"  {DIM}{stripped}{RESET}")
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n{YELLOW}[fi-rig] Shutting down...{RESET}")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        ok("Server stopped. Goodbye.")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    enable_windows_ansi()
    print(BANNER)

    check_python()
    install_packages()

    proc = start_server()
    wait_for_server(proc)
    open_browser()
    run_until_exit(proc)

if __name__ == "__main__":
    main()
