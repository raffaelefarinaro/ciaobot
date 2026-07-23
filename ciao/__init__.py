"""Ciaobot personal assistant server."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("ciaobot")
except PackageNotFoundError:
    __version__ = "0.5.2"
