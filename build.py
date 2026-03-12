#!/usr/bin/env python3
"""
Build script for SBCOSInstaller.
Creates a standalone binary using PyInstaller for the current platform.
Output goes to releases/.

Usage:
    python3 build.py
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
RELEASES_DIR = ROOT / "releases"
VENV_DIR = ROOT / ".venv"


def _in_venv():
    return sys.prefix != sys.base_prefix


def _relaunch_in_venv():
    print("Creating virtual environment with system site-packages...")
    subprocess.check_call([
        sys.executable, "-m", "venv",
        "--system-site-packages", str(VENV_DIR),
    ])
    if platform.system() == "Windows":
        venv_python = VENV_DIR / "Scripts" / "python.exe"
    else:
        venv_python = VENV_DIR / "bin" / "python3"
    print(f"Re-launching build inside venv: {venv_python}")
    os.execv(str(venv_python), [str(venv_python), __file__] + sys.argv[1:])


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def get_output_name():
    system = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        machine = "x86_64"
    elif machine in ("aarch64", "arm64"):
        machine = "arm64"

    if system == "windows":
        return f"SBCOSInstaller-{machine}.exe"
    else:
        return f"SBCOSInstaller-{machine}"


def build():
    # Skip venv in CI — dependencies are already installed globally
    if not _in_venv() and not os.environ.get("CI"):
        _relaunch_in_venv()

    ensure_pyinstaller()

    RELEASES_DIR.mkdir(exist_ok=True)

    output_name = get_output_name()
    separator = ";" if platform.system() == "Windows" else ":"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", output_name,
        "--noconfirm",
        "--clean",
    ]

    # Bundle data files
    if (ROOT / "resources").is_dir():
        cmd += ["--add-data", f"resources{separator}resources"]

    # Hidden imports for PyGObject/GTK3
    for module in [
        "gi",
        "gi.repository.Gtk",
        "gi.repository.Gdk",
        "gi.repository.GdkPixbuf",
        "gi.repository.GLib",
        "gi.repository.Pango",
    ]:
        cmd += ["--hidden-import", module]

    # Use icon for the binary — Windows needs .ico, Linux uses .png
    if platform.system() == "Windows":
        icon_file = ROOT / "resources" / "icon.ico"
    else:
        icon_file = ROOT / "resources" / "icon.png"
    if icon_file.exists():
        cmd += ["--icon", str(icon_file)]

    cmd.append("main.py")

    print(f"Building {output_name}...")
    print(f"Command: {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=str(ROOT))

    built = ROOT / "dist" / output_name
    dest = RELEASES_DIR / output_name

    if dest.exists():
        dest.unlink()
    shutil.move(str(built), str(dest))

    for d in (ROOT / "build", ROOT / "dist"):
        if d.exists():
            shutil.rmtree(d)
    spec_file = ROOT / f"{output_name}.spec"
    if spec_file.exists():
        spec_file.unlink()

    print(f"Build complete: {dest}")
    print(f"Size: {dest.stat().st_size / (1024 * 1024):.1f} MB")


if __name__ == "__main__":
    build()
