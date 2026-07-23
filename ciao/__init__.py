"""Ciaobot personal assistant server."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

# Kept in sync by the release tool, which bumps this top-level literal
# (ciao/release.py matches ``^__version__ = "..."``). When the package is
# installed the real distribution version overrides it below; the literal is
# the fallback for running straight from a source checkout.
__version__ = "0.5.3"
try:
    __version__ = version("ciaobot")
except PackageNotFoundError:
    pass
