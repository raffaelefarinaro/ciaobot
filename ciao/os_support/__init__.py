"""Operating-system seams that are not provider-specific.

File locks, descriptor opens, the user key, process trees, private files, the
terminal and the tool PATH live in this package, so each has one implementation
and one set of tests and call sites never branch on ``sys.platform`` themselves.
"""
