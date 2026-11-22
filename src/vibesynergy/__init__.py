"""VibeSynergy: Personal Health & Fitness Store."""

from importlib.metadata import PackageNotFoundError, version

from vibesynergy.releases import VERSION

# The version people see (``0.1.0-beta.0.1``, #140): vibesynergy.releases. The package keeps a
# PEP 440 version for the line (pyproject.toml), which is not shown.
__version__ = VERSION

try:
    __package_version__ = version("vibesynergy")
except PackageNotFoundError:  # running from a source tree without installation
    __package_version__ = "0.0.0"
