"""Build standalone, click-to-run packages for Quarto:
- macOS: dist/Quarto.app + dist/Quarto-macOS.dmg (with /Applications symlink)
- Windows: dist/Quarto-Windows.exe (single windowed executable, custom icon)

Never bundles configs/local/, .env, runs/, or users*.csv.
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAFE_CONFIGS = ("enterprise-development.yaml", "targets.yaml")


def read_version():
    ns = {}
    with open(os.path.join(ROOT, "quarto", "__init__.py"), encoding="utf-8") as fh:
        exec(fh.read(), ns)
    return ns.get("__version__", "2.0.0")


def stage_clean_configs():
    stage = os.path.join(ROOT, "build", "staging_configs")
    if os.path.exists(stage):
        shutil.rmtree(stage)
    os.makedirs(stage, exist_ok=True)
    src_dir = os.path.join(ROOT, "configs")
    for name in SAFE_CONFIGS:
        src = os.path.join(src_dir, name)
        if not os.path.exists(src):
            raise FileNotFoundError(f"Missing public template config: {src}")
        shutil.copy2(src, os.path.join(stage, name))
    return stage


def build(target):
    try:
        import PyInstaller.__main__  # noqa: F401
    except ImportError:
        raise SystemExit("PyInstaller is required. Install it with: pip install pyinstaller")

    version = read_version()
    cfg_stage = stage_clean_configs()
    assets_dir = os.path.join(ROOT, "assets")
    static_dir = os.path.join(ROOT, "quarto", "ui", "static")
    entry = os.path.join(ROOT, "quarto", "ui", "__main__.py")
    dist_dir = os.path.join(ROOT, "dist")
    work_dir = os.path.join(ROOT, "build", "pyi")
    spec_dir = os.path.join(ROOT, "build")
    pyi_cache = os.path.join(ROOT, "build", "pyi_cache")
    os.makedirs(pyi_cache, exist_ok=True)
    build_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=pyi_cache)
    sep = ";" if os.name == "nt" else ":"

    if target == "macos":
        icns = os.path.join(assets_dir, "Quarto.icns")
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", "Quarto",
            "--osx-bundle-identifier", "io.github.quarto.app",
            "--icon", icns,
            "--add-data", f"{static_dir}{sep}quarto/ui/static",
            "--add-data", f"{cfg_stage}{sep}configs",
            "--add-data", f"{assets_dir}{sep}assets",
            "--distpath", dist_dir,
            "--workpath", work_dir,
            "--specpath", spec_dir,
            entry,
        ]
        subprocess.run(cmd, check=True, cwd=ROOT, env=build_env)

        app_path = os.path.join(dist_dir, "Quarto.app")
        plist_path = os.path.join(app_path, "Contents", "Info.plist")
        if os.path.exists(plist_path):
            with open(plist_path, "rb") as fh:
                plist = plistlib.load(fh)
            plist.update({
                "CFBundleName": "Quarto",
                "CFBundleDisplayName": "Quarto",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
                "NSHighResolutionCapable": True,
            })
            with open(plist_path, "wb") as fh:
                plistlib.dump(plist, fh)

        dmg_stage = os.path.join(ROOT, "build", "dmg_stage")
        if os.path.exists(dmg_stage):
            shutil.rmtree(dmg_stage)
        os.makedirs(dmg_stage, exist_ok=True)
        shutil.copytree(app_path, os.path.join(dmg_stage, "Quarto.app"), symlinks=True)
        os.symlink("/Applications", os.path.join(dmg_stage, "Applications"))
        with open(os.path.join(dmg_stage, "README - First Launch.txt"), "w", encoding="utf-8") as fh:
            fh.write(
                "Quarto — Standalone Desktop App\n"
                "==================================\n\n"
                "1. Drag Quarto.app onto the Applications folder icon (or double-click Quarto.app directly).\n"
                "2. First-time macOS Gatekeeper note: if macOS blocks the app as from an unidentified developer,\n"
                "   open System Settings -> Privacy & Security -> scroll down and click Open Anyway.\n"
                "3. Your browser will open automatically.\n"
                "4. All migration logs, CSVs, and run folders are stored cleanly in ~/Documents/Quarto.\n"
                "5. To stop the background server completely, click Quit in the top-right corner of the app\n"
                "   or right-click the Quarto icon in the macOS Dock -> Quit.\n"
            )
        dmg_path = os.path.join(dist_dir, "Quarto-macOS.dmg")
        if os.path.exists(dmg_path):
            os.remove(dmg_path)
        rc = subprocess.run([
            "diskutil", "image", "create", "from",
            "--volumeName", "Quarto",
            "--format", "UDZO",
            dmg_stage,
            dmg_path,
        ], check=False).returncode
        if rc != 0:
            subprocess.run([
                "hdiutil", "create",
                "-volname", "Quarto",
                "-srcfolder", dmg_stage,
                "-ov", "-format", "UDZO",
                dmg_path,
            ], check=True)
        print(f"Built macOS bundle: {app_path}")
        print(f"Built macOS DMG:    {dmg_path}")
        return dmg_path

    if target == "windows":
        ico = os.path.join(assets_dir, "Quarto.ico")
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile", "--windowed",
            "--name", "Quarto-Windows",
            "--icon", ico,
            "--add-data", f"{static_dir}{sep}quarto/ui/static",
            "--add-data", f"{cfg_stage}{sep}configs",
            "--add-data", f"{assets_dir}{sep}assets",
            "--distpath", dist_dir,
            "--workpath", work_dir,
            "--specpath", spec_dir,
            entry,
        ]
        subprocess.run(cmd, check=True, cwd=ROOT, env=build_env)
        exe_path = os.path.join(dist_dir, "Quarto-Windows.exe")
        print(f"Built Windows EXE:  {exe_path}")
        return exe_path

    raise SystemExit(f"Unsupported target: {target}")


def main():
    default = "macos" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux")
    p = argparse.ArgumentParser(description="Build standalone Quarto app packages.")
    p.add_argument("--target", choices=["macos", "windows"], default=default)
    args = p.parse_args()
    build(args.target)


if __name__ == "__main__":
    main()
