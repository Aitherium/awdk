# Skill: Debugging

A systematic procedure for finding and fixing bugs in code.

## When to use

When code produces unexpected behavior, crashes, or fails tests. Use this skill
to isolate the root cause and fix it.

## Procedure

1. **Reproduce the bug.** Get:
   - Error message or stack trace (paste it completely)
   - Steps to reproduce (what input/action triggers it?)
   - Expected behavior vs. actual behavior
   - When did it start? (regression in a recent commit?)

2. **Isolate the scope.** Determine:
   - Is it a specific input or universal? (try different inputs)
   - Is it a specific environment? (local only, staging only, production?)
   - Is it recent (regression) or has it always been broken?
   - Which component is failing? (narrow from whole app to single function)

3. **Read the error stack trace.** Understand:
   - Where did the crash happen? (file, line, function)
   - What was the execution path? (which functions called which)
   - What was the state? (variable values, function arguments)
   - What was the assumption that broke? (why did the code expect something
     different?)

4. **Examine the code.** Look at:
   - The line that crashed (what does it do?)
   - The function it's in (what's the pre/post condition?)
   - Error handling in that path (did someone forget to validate input?)
   - Related code (similar patterns, shared state)

5. **Add logging/debugging.** Narrow down the cause:
   - Print intermediate values (does x have the value I expected?)
   - Use a debugger to step through the code
   - Write a minimal test that reproduces the bug (smaller = faster debug)

6. **Formulate a hypothesis.** State:
   - "The bug is: [specific thing that's wrong]"
   - "The root cause is: [why the code is wrong]"
   - "The fix is: [code change to correct it]"

7. **Implement the fix.** Apply the minimal change needed:
   - Do not refactor the whole function
   - Do not "fix" unrelated issues
   - Make the fix precise and reviewable

8. **Verify the fix.** Prove it works:
   - Run the minimal test case that reproduced the bug
   - Run the full test suite
   - Test in the same environment where the bug occurred
   - Add a regression test so this bug never returns

## Quality bar

- You have identified the root cause, not just a symptom.
- The fix is minimal and doesn't introduce new risks.
- You have a test that proves the fix works and prevents regression.
- You understand why the original code was wrong (not just how to fix it).
