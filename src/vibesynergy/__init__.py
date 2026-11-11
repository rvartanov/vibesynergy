"""VibeSynergy: Personal Health & Fitness Store."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vibesynergy")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"
