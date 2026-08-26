#!/usr/bin/env python3
"""Back-compat shim for the old GWS headless auth helper.

The canonical entry point is now `ciao gws-auth-helper` (which ships inside the
installed app). This thin shim forwards to it so dev checkouts and any external
references keep working unchanged.
"""

import os
import sys


def main() -> int:
    from ciao.gws_auth_helper import main_entry

    return main_entry(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
