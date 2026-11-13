"""PyInstaller entry point for the CLI / server binary."""

import sys

from icogenerator.cli import main

if __name__ == "__main__":
    sys.exit(main())
