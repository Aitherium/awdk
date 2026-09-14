# Skill: Feature Development

A procedure for shipping production-quality features: plan, code, test, review,
ship.

## When to use

When you have a feature request, user story, or capability to build into your
codebase.

## Procedure

1. **Understand the requirement.** Ask:
   - What does the user need to accomplish?
   - What edge cases matter? (what if input is empty/None/huge?)
   - Any compliance or performance requirements?
   - How will this be used? (who, how often, dependencies)
   - Is there existing code to build on or integrate with?

2. **Read existing code.** Examine:
   - Related files and functions (to avoid duplication and learn conventions)
   - API contracts (how functions should behave)
   - Error handling patterns used in the codebase
   - Test structure and naming conventions

3. **Plan the changes.** Outline:
   - Which files will you create/modify?
   - What functions/classes will you add or change?
   - Any database migrations, config changes, or deployment steps?
   - How will you test this?

4. **Implement incrementally.** Write code in small, testable chunks:
   - Implement core logic
   - Add error handling (anticipate what can go wrong)
   - Add parameter validation
   - Write docstrings for public functions

5. **Write tests as you code.** For each function:
   - Happy path test (normal input, expected output)
   - Error cases (None, empty, invalid input)
   - Edge cases (boundary values, large inputs)
   - Integration tests if the feature touches multiple systems

6. **Run the full test suite.** Verify:
   - Your new tests pass
   - No existing tests broke
   - Code linter/formatter pass

7. **Review your diff.** Before committing:
   - Read every line you changed
   - Spot simplifications or naming improvements
   - Check for security issues (input validation, SQL injection, auth)
   - Look for dead code or debug statements left in

8. **Commit with a clear message.** Include:
   - What you built (one line)
   - Why (what problem does it solve?)
   - Any gotchas or follow-up work

## Quality bar

- All tests pass, including new tests for edge cases.
- Code follows project conventions (naming, style, imports).
- No bare exceptions or silently-failing error paths.
- Public functions have docstrings.
- Commit message is clear and reviewable.
