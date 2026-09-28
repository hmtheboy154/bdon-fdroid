"""Allow ``python3 -m bdon_fdroid``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
