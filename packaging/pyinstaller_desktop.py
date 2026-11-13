"""PyInstaller entry point for the desktop GUI binary."""

import sys

from icogenerator.desktop import main

if __name__ == "__main__":
    sys.exit(main())
