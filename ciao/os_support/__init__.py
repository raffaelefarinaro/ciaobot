"""Operating-system differences that are not provider-specific.

Every POSIX/Windows difference the engine depends on lives in this package
(file locks, descriptor opens and the user key today; process trees, signals,
atomic replace, private files and directory links as the Windows port lands,
see #696). Call sites import from here and never branch on ``sys.platform``
themselves, so each difference has one implementation and one set of tests.
"""
