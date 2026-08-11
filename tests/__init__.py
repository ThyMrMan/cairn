"""Marks `tests` as a real package.

Not optional, and not cosmetic. pywb ships its own top-level `tests` package
into site-packages, and a *regular* package anywhere on the path beats a
namespace directory at the front of it — so without this file every
`from tests.conftest import …` in the container resolves to pywb's tests and
the whole suite fails to collect. It passes on a development machine, where
pywb is not installed, which is the worst possible place for the difference
to live.
"""
