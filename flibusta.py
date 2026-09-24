#!/usr/bin/env python3
"""Command line entry point for the Flibusta downloader."""

import sys

from fli_app.cli import main


if __name__ == "__main__":
    sys.exit(main())
