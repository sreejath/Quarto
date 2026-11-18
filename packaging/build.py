"""Build standalone, click-to-run packages for Jira2ADO:
- macOS: dist/Jira2ADO.app + dist/Jira2ADO-macOS.dmg (with /Applications symlink)
- Windows: dist/Jira2ADO-Windows.exe (single windowed executable, custom icon)

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
FORBIDDEN = ("petvalu", "petretailbrands", "fin corp", "fin-corp", "dea-", "customfield_10330")


def read_version():
    ns = {}
    with open(os.path.join(ROOT, "jira2ado", "__init__.py"), encoding="utf-8") as fh:
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
        with open(src, encoding="utf-8") as fh:
            text = fh.read().lower()
        for bad in FORBIDDEN:
            if bad in text:
                raise RuntimeError(f"Refusing to bundle {name}: matched forbidden string '{bad}'")
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
    static_dir = os.path.join(ROOT, "jira2ado", "ui", "static")
    entry = os.path.join(ROOT, "jira2ado", "ui", "__main__.py")
    dist_dir = os.path.join(ROOT, "dist")
    work_dir = os.path.join(ROOT, "build", "pyi")
    spec_dir = os.path.join(ROOT, "build")
    pyi_cache = os.path.join(ROOT, "build", "pyi_cache")
    os.makedirs(pyi_cache, exist_ok=True)
    build_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=pyi_cache)
    sep = ";" if os.name == "nt" else ":"

    if target == "macos":
        icns = os.path.join(assets_dir, "Jira2ADO.icns")
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", "Jira2ADO",
            "--osx-bundle-identifier", "io.github.jira2ado.app",
            "--icon", icns,
            "--add-data", f"{static_dir}{sep}jira2ado/ui/static",
            "--add-data", f"{cfg_stage}{sep}configs",
            "--add-data", f"{assets_dir}{sep}assets",
            "--distpath", dist_dir,
            "--workpath", work_dir,
            "--specpath", spec_dir,
            entry,
        ]
        subprocess.run(cmd, check=True, cwd=ROOT, env=build_env)

        app_path = os.path.join(dist_dir, "Jira2ADO.app")
        plist_path = os.path.join(app_path, "Contents", "Info.plist")
        if os.path.exists(plist_path):
            with open(plist_path, "rb") as fh:
                plist = plistlib.load(fh)
            plist.update({
                "CFBundleName": "Jira2ADO",
                "CFBundleDisplayName": "Jira2ADO",
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
        shutil.copytree(app_path, os.path.join(dmg_stage, "Jira2ADO.app"), symlinks=True)
        os.symlink("/Applications", os.path.join(dmg_stage, "Applications"))
        with open(os.path.join(dmg_stage, "README - First Launch.txt"), "w", encoding="utf-8") as fh:
            fh.write(
                "Jira2ADO — Standalone Desktop App\n"
                "==================================\n\n"
                "1. Drag Jira2ADO.app onto the Applications folder icon (or double-click Jira2ADO.app directly).\n"
                "2. First-time macOS Gatekeeper note: if macOS warns about an unidentified developer,\n"
                "   Right-click (Control-click) Jira2ADO.app -> click Open -> click Open.\n"
                "3. Your browser will open automatically.\n"
                "4. All migration logs, CSVs, and run folders are stored cleanly in ~/Documents/Jira2ADO.\n"
                "5. To stop the background server completely, click Quit in the top-right corner of the app\n"
                "   or right-click the Jira2ADO icon in the macOS Dock -> Quit.\n"
            )
        dmg_path = os.path.join(dist_dir, "Jira2ADO-macOS.dmg")
        if os.path.exists(dmg_path):
            os.remove(dmg_path)
        rc = subprocess.run([
            "diskutil", "image", "create", "from",
            "--volumeName", "Jira2ADO",
            "--format", "UDZO",
            dmg_stage,
            dmg_path,
        ], check=False).returncode
        if rc != 0:
            subprocess.run([
                "hdiutil", "create",
                "-volname", "Jira2ADO",
                "-srcfolder", dmg_stage,
                "-ov", "-format", "UDZO",
                dmg_path,
            ], check=True)
        print(f"Built macOS bundle: {app_path}")
        print(f"Built macOS DMG:    {dmg_path}")
        return dmg_path

    if target == "windows":
        ico = os.path.join(assets_dir, "Jira2ADO.ico")
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile", "--windowed",
            "--name", "Jira2ADO-Windows",
            "--icon", ico,
            "--add-data", f"{static_dir}{sep}jira2ado/ui/static",
            "--add-data", f"{cfg_stage}{sep}configs",
            "--add-data", f"{assets_dir}{sep}assets",
            "--distpath", dist_dir,
            "--workpath", work_dir,
            "--specpath", spec_dir,
            entry,
        ]
        subprocess.run(cmd, check=True, cwd=ROOT, env=build_env)
        exe_path = os.path.join(dist_dir, "Jira2ADO-Windows.exe")
        print(f"Built Windows EXE:  {exe_path}")
        return exe_path

    raise SystemExit(f"Unsupported target: {target}")


def main():
    default = "macos" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux")
    p = argparse.ArgumentParser(description="Build standalone Jira2ADO app packages.")
    p.add_argument("--target", choices=["macos", "windows"], default=default)
    args = p.parse_args()
    build(args.target)


if __name__ == "__main__":
    main()
