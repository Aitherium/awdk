"""Cognition frameworks: the platform's context-change loop as in-process libraries.

Design: ``AitherOS/docs/cognition-frameworks-design.md`` (section 9 is the first
vertical slice). Each module is the library a fleet service hosts:

* :mod:`.bus`        -- Flux Stream consumer group + Strata path store (in-process)
* :mod:`.hub`        -- CNS context hub: ops, deterministic merge, record, neuron waves
* :mod:`.perceive`   -- Sense ``/perceive``: facts with observation evidence
* :mod:`.worldmodel` -- world model ``/rules/verify``: rules as code, replay-verified
* :mod:`.daydream`   -- Daydream ``/daydreams/plan`` + the contract audit
* :mod:`.slumber`    -- Slumber ``consolidate_contracts()``
* :mod:`.slice`      -- the nine-hop slice (classroom -> ... -> Evolution score)

The record, ``permits()`` and the invariants are
:mod:`adk.reasoning.solve.context`; nothing here defines a second permission function.
"""
