"""Add Ciaobot's bundled dependency tree to this interpreter's ``sys.path``.

Copied into the bundled interpreter's own site-packages as
``ciao_bundled_site.py`` by ``scripts/build-bundled-runtime.sh``, alongside a
``.pth`` file that imports it while ``site`` runs.

The app ships one dependency tree per architecture
(``<runtime>/site-packages/<arch>``) next to two interpreters. Attaching that
tree here rather than exporting ``PYTHONPATH`` from the launcher is what keeps
it from leaking: ``PYTHONPATH`` is inherited by every descendant process, so a
child that runs a different Python - a Homebrew or repo-venv ``ciao``, which is
what a shell profile running ``brew shellenv`` puts first on ``PATH`` - would
import these packages and then fail on the CPython 3.12 extension modules with
``No module named 'pydantic_core._pydantic_core'``. Only this interpreter reads
this file, so only this interpreter gets the tree.

The relative path is substituted at build time; keeping it relative lets the
signed bundle be installed under either ``/Applications`` or ``~/Applications``.
"""

from __future__ import annotations

import os
import site

# Replaced by scripts/build-bundled-runtime.sh with the path from this
# interpreter's site-packages to its architecture's dependency tree.
_BUNDLED_SITE_REL = "@BUNDLED_SITE_REL@"


def _attach() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.normpath(os.path.join(here, _BUNDLED_SITE_REL))
    if os.path.isdir(bundled):
        site.addsitedir(bundled)


_attach()
