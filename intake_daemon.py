"""Entry point for the chat intake daemon."""

import sys

from intake.daemon import main

if __name__ == "__main__":
    sys.exit(main())