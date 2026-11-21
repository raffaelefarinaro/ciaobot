"""The environment a child process the engine starts inherits.

The bundled Mac engine is started by ``bin/ciao`` (``mac/scripts/engine-launcher.sh``), which
exports ``SSL_CERT_FILE`` (the certifi bundle the engine's own TLS trusts) and
``PYTHONDONTWRITEBYTECODE=1`` for the engine's sake. Neither belongs in a child: an ACP agent,
a terminal, a scheduled command or a provider probe would trust only certifi and would never
write bytecode into its own tree. ``child_env`` returns a copy with both removed when the engine
is the bundled app. The engine's own ``os.environ`` is never changed, so its httpx TLS keeps
using certifi, and outside the bundle nothing is removed.

``adapter_install`` builds npm's environment from ``os.environ`` on purpose (npm must verify TLS
against the same bundle), so it does not call this helper.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# The two variables the bundled launcher exports. Nothing else the launcher sets is scrubbed:
# its other unsets (PYTHON*, DYLD_*, NODE_OPTIONS and the rest) are already absent from the
# engine's environment, and the child keeps whatever the user's own shell provides.
BUNDLED_ENGINE_SCRUBBED_KEYS: tuple[str, ...] = ("SSL_CERT_FILE", "PYTHONDONTWRITEBYTECODE")


def child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child's environment: ``source`` (default ``os.environ``) minus the bundle's exports.

    Returns a new dict. Only the bundled engine removes the keys; every other install mode
    returns the source unchanged.
    """
    env = dict(os.environ if source is None else source)
    if _running_bundled_engine():
        for key in BUNDLED_ENGINE_SCRUBBED_KEYS:
            env.pop(key, None)
    return env


def _running_bundled_engine() -> bool:
    # Imported here: package_version imports this package's processes module at load time.
    from ciao import package_version

    return package_version.detect_install_mode() == "bundled_app"
