"""Enables `python -m urlcheck`."""
import sys

if __package__:
    from .urlcheck import main
else:
    from urlcheck import main

if __name__ == "__main__":
    sys.exit(main())
