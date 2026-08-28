#!/usr/bin/env python3
"""Back-compat shim for the old GWS headless auth helper.

The canonical entry point is now `ciao gws-auth-helper` (which ships inside the
installed app). This thin shim forwards to it so dev checkouts and any external
references keep working unchanged.
"""

import os
import sys


def main() -> int:
    # Preserve the checkout root when invoked from a dev checkout, so
    # `ciao gws-auth-helper` resolves the credential directory there on hosts
    # without a macOS LaunchAgent plist. Only sets it when unset, mirroring the
    # bash shim and the old __file__-anchored lookup.
    if not os.environ.get("CIAO_WORKSPACE"):
        checkout = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.environ["CIAO_WORKSPACE"] = checkout
    from ciao.gws_auth_helper import main_entry

    return main_entry(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
