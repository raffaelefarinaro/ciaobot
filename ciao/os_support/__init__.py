"""Operating-system differences that are not provider-specific.

Every POSIX/Windows difference the engine depends on lives in this package
(file locks, descriptor opens, the user key, process trees, private files and
the terminal PATH today; signals and atomic replace as the Windows port lands,
see #696). Call sites import from here and never branch on ``sys.platform``
themselves, so each difference has one implementation and one set of tests.
"""
