#!/usr/bin/env python
"""Build llama-shell.exe manually to work around PyInstaller BeginUpdateResource issues.

Steps:
1. Run PyInstaller to generate the PKG file (skipping exe creation)
2. Manually create the exe from bootloader + PKG
"""
import os
import sys
import glob
import shutil
import subprocess

VENV = r"D:\llama-shell-work\venv\Scripts\python"
SRC_DIR = r"D:\llama-shell-work\src"
DIST_DIR = os.path.join(SRC_DIR, "dist")
BUILD_DIR = os.path.join(SRC_DIR, "build")
DIST_EXE = os.path.join(DIST_DIR, "llama-shell.exe")
BOOTLOADER = r"D:\llama-shell-work\venv\Lib\site-packages\PyInstaller\bootloader\Windows-64bit-intel\runw.exe"

def main():
    print("Step 1: Running PyInstaller to generate PKG...")
    # Run PyInstaller with --onedir to generate the PKG in the build directory
    # We'll use the PKG from the build directory
    cmd = [
        VENV, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", "llama-shell",
        "--add-data", "recommend.py;.",
        "--add-data", "gguf_fast.py;.",
        "--add-data", "gguf_parse.py;.",
        "--distpath", DIST_DIR,
        "--workpath", BUILD_DIR,
        os.path.join(SRC_DIR, "gui.py"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=SRC_DIR)
    
    if result.returncode != 0:
        print(f"PyInstaller failed:\n{result.stderr[:2000]}")
        # Try to continue if PKG was still generated
        pkg_files = glob.glob(os.path.join(BUILD_DIR, "llama-shell", "*.pkg"))
        if not pkg_files:
            print("No PKG file found - cannot continue")
            sys.exit(1)
    
    print("Step 2: Finding PKG file...")
    pkg_files = glob.glob(os.path.join(BUILD_DIR, "llama-shell", "*.pkg"))
    if not pkg_files:
        print(f"ERROR: No PKG file found in {BUILD_DIR}/llama-shell/")
        sys.exit(1)
    
    pkg_path = pkg_files[0]
    print(f"  Found: {pkg_path} ({os.path.getsize(pkg_path)} bytes)")
    
    print("Step 3: Creating exe from bootloader + PKG...")
    if os.path.exists(DIST_EXE):
        os.remove(DIST_EXE)
    
    with open(BOOTLOADER, 'rb') as f:
        bootloader_data = f.read()
    
    with open(pkg_path, 'rb') as f:
        pkg_data = f.read()
    
    with open(DIST_EXE, 'wb') as f:
        f.write(bootloader_data)
        f.write(pkg_data)
    
    final_size = os.path.getsize(DIST_EXE)
    print(f"  Created: {DIST_EXE} ({final_size} bytes)")
    print(f"    bootloader: {len(bootloader_data)}")
    print(f"    pkg: {len(pkg_data)}")
    
    print("\nBuild complete!")
    print(f"Run: {DIST_EXE}")

if __name__ == "__main__":
    main()
