"""Allow ``python -m snipvault``."""

import sys

from snipvault.cli import main

if __name__ == "__main__":
    sys.exit(main())
