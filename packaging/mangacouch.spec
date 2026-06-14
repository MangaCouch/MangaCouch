# PyInstaller spec for the portable --onedir build (§7.1).
#
# --onedir (NOT --onefile): the app folder sits beside database/ cache/ manga/ and runs in place.
# UTF-8 mode is baked in. The bundled SPA (src/mangacouch/web) and the native libs from
# pyvips[binary]/pypdfium2 are collected automatically.

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

datas = collect_data_files("mangacouch")  # includes src/mangacouch/web (the built SPA)
datas += copy_metadata("mangacouch")
binaries = collect_dynamic_libs("pyvips") + collect_dynamic_libs("pypdfium2")

hiddenimports = [
    "mangacouch.plugins.builtin.ehentai_login",
    "mangacouch.plugins.builtin.ehentai_download",
    "mangacouch.plugins.builtin.ehentai_metadata",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]


a = Analysis(
    ["entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mangacouch",
    console=True,
    # Bake UTF-8 mode into the frozen interpreter (R7 / §3.4).
    runtime_tmpdir=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="mangacouch",
)
