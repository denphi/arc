"""ARC integration package for the cloned Co-Scientist implementation.

This package is intentionally a thin adapter layer. The upstream clone lives
at ``./Co-Scientist`` and is not modified by ARC; imports of that code are
lazy so ARC can load this package even when Co-Scientist's optional runtime
dependencies are not installed.
"""

