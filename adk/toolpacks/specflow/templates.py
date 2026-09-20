"""Scaffold templates for a specflow change. Own prose; the SHAPE (proposal ->
delta specs -> design -> tasks) follows OpenSpec, the constitution check and
prioritised user stories follow spec-kit. HTML comments are guidance and are
ignored by the validator, so a section that holds only a comment is empty."""
from __future__ import annotations

PROPOSAL = """# Proposal: {change_id}

## Why

{why}

## What Changes

<!-- Bullet the concrete changes. Mark breaking ones **BREAKING**. -->
-

## Capabilities

### New Capabilities
<!-- One line per capability this change introduces. Each needs a delta spec at
     specs/<capability>/spec.md with an "## ADDED Requirements" section.
     Use kebab-case names; nested paths (identity/user-auth) are allowed. -->
-

### Modified Capabilities
<!-- Existing capabilities whose REQUIREMENTS change (not just their code). Each
     needs a delta spec with "## MODIFIED Requirements" / "## REMOVED Requirements".
     Leave empty if no requirement changes. A change with no capabilities at all
     (refactor, tooling, docs) sets `skip_specs: true` in .openspec.yaml instead
     of inventing a requirement. -->
-

## User Stories

<!-- Prioritised, and each one independently testable: a story is done when its
     acceptance line holds on its own, with no other story in place. P1 is the
     thinnest slice that delivers value; ship it first. -->

### P1: <story name>
As a <who>, I want <what>, so that <why>.
- **WHEN** <the user does or the system receives ...>
- **THEN** <the observable result ...>

### P2: <story name>
As a <who>, I want <what>, so that <why>.
- **WHEN** ...
- **THEN** ...
{constitution}
## Impact

<!-- Affected code, APIs, data, dependencies, operations. -->
-
"""

WHY_PLACEHOLDER = "<!-- 1-3 sentences: the problem or opportunity, and why now. -->"

CONSTITUTION_SECTION = """
## Constitution check

<!-- openspec/CONSTITUTION.md exists. For each principle, state how this proposal
     honours it, or name the deliberate exception and why. -->
-
"""

DELTA_SPEC = """# Spec Delta: {capability}

## Purpose
<!-- New capabilities only: one or two sentences on what this capability is for.
     Delete this section when the capability already has a living spec. -->

## ADDED Requirements

### Requirement: <name>
<!-- The requirement as observable behaviour, using SHALL / MUST. -->
The system SHALL ...

#### Scenario: <name>
- **WHEN** <condition>
- **THEN** <expected outcome>

## MODIFIED Requirements
<!-- Blocks here REPLACE the living block with the same "### Requirement:" name.
     Copy the whole block and edit it; the name must already exist. -->

## REMOVED Requirements
<!-- "### Requirement: <name>" plus one line on why it goes. The name must exist. -->
"""

DESIGN = """# Design: {change_id}

## Context

<!-- Current state and constraints that shape the approach. proposal.md holds the
     motivation; do not restate it. -->

## Goals / Non-Goals

**Goals:**
-

**Non-Goals:**
-

## Decisions

<!-- Each decision with its rationale and the alternatives considered. -->

## Risks / Trade-offs

-
"""

TASKS = """# Tasks: {change_id}

<!-- Groups are "## N. Name"; tasks are "- [ ] N.M text". Tick with spec_tasks
     done=N.M. Keep each task small enough to verify on its own. -->

## 1. Implementation

- [ ] 1.1 Describe the first task

## 2. Verification

- [ ] 2.1 Tests cover every WHEN/THEN scenario in the delta specs
"""

OPENSPEC_YAML = """# Per-change settings read by spec_validate.
# skip_specs: true  -> a change with no requirement deltas (refactor, tooling,
#                      docs) is allowed to have no specs/ files.
skip_specs: false
"""
