# PyInstaller spec for the standalone executable; run through scripts/build_standalone.py, which sets
# MANGACOUCH_VERSION and MANGACOUCH_ICON (an .icns/.ico file, macOS and Windows only).
#
# Every platform gets a `mangacouch/` folder with the `mangacouch` CLI. On Windows and macOS the same
# executable started without arguments opens the tray launcher (mangacouch.desktop):
#   - Windows: a console program that hides its console when it owns it (double-clicked), so the CLI
#     keeps working in a terminal;
#   - macOS: additionally wrapped as MangaCouch.app, a menu bar app without a Dock icon.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

DESKTOP = sys.platform in ("darwin", "win32")
VERSION = os.environ["MANGACOUCH_VERSION"]
ICON = os.environ.get("MANGACOUCH_ICON") or None

# uvicorn picks its protocol and loop implementations by name at run time; pyvips only uses the bundled
# libvips when its optional _libvips extension imports.
hidden = [*collect_submodules("uvicorn"), "_libvips"]
if DESKTOP:
    hidden += collect_submodules("mangacouch.desktop")
if sys.platform == "darwin":
    # Imported by name at run time (mangacouch.desktop.tray.objc).
    hidden += ["AppKit", "Foundation", "PyObjCTools.AppHelper"]

a = Analysis(
    [os.path.join(SPECPATH, "standalone_entry.py")],
    # The built web bundle (run `npm --prefix web run build` first) and the package version.
    datas=[*collect_data_files("mangacouch"), *copy_metadata("mangacouch")],
    hiddenimports=hidden,
    # Linux builds have no desktop launcher; keep Tk out of them.
    excludes=[] if DESKTOP else ["tkinter", "_tkinter", "PIL", "pystray", "mangacouch.desktop"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mangacouch",
    console=sys.platform != "darwin",
    hide_console="hide-early" if sys.platform == "win32" else None,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, name="mangacouch")
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MangaCouch.app",
        icon=ICON,
        bundle_identifier="io.github.mangacouch",
        version=VERSION,
        info_plist={
            "CFBundleName": "MangaCouch",
            "CFBundleDisplayName": "MangaCouch",
            "CFBundleShortVersionString": VERSION,
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
        },
    )
