"""The h30 reasoning core, vendored at a pinned commit (not edited here).

Each module carries a one-line provenance header and is otherwise the h30 file
byte for byte, except for three keyword-only seams marked ``SEAM(adk)``
(``ReasoningLoop(sink=, governor=)`` and ``Sandbox(cancel_check=)``) whose
defaults keep the h30 behaviour. ``adk.reasoning.solve._provenance.PINNED_SOURCE``
records the h30 sha and the hashes; ``tests/test_reasoning_solve_pinned.py``
fails on a hand edit or on drift. Sync only by re-vendoring at a new sha.

These modules import numpy at top level; nothing imports this package until a
loop is built.
"""
