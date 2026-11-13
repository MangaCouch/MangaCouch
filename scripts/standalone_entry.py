"""PyInstaller entry point for the standalone executable; it runs the same CLI as `python -m mangacouch`."""

from mangacouch.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
