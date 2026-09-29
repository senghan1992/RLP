"""`python -m rlp_svc` -> the CLI. Same entry the `rlp-svc` console script uses."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
