"""Record the help videos: python run.py [scenario ...]."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scenarios  # noqa: F401  (registers the scenarios)
from rec import run

run(sys.argv[1:])
