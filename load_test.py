"""Launcher: the code lives in src/. Run `python load_test.py` from the project folder."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import load_test  # noqa: E402  (resolves to src/load_test.py)

if __name__ == "__main__":
    load_test.main()
