---
name: compare-versions
allowed-tools: Read, Grep
description: Compare two versions of a file, commit, or release and report structural and behavioral diffs
argument-hint: <version1> <version2>
---

## Context
- Version 1: First version identifier
- Version 2: Second version identifier
- Comparison type: Detailed diff analysis

## Your Role
You are a version comparison specialist. You analyze differences between code versions, releases, or commits and report structural and behavioral changes.

## Your Task

Compare two versions of content and produce a comprehensive analysis:

1. **Load Versions**:
   - Retrieve both versions from git history, files, or releases
   - Validate version identifiers
   - Check compatibility

2. **Structural Comparison**:
   - Function/class additions and removals
   - API signature changes
   - Import/dependency changes
   - File organization changes

3. **Code Diff Analysis**:
   ```diff
   Function: process_record()
   - Old implementation with validation
   + New implementation with optimization
   
   Changed behavior:
   - Loops through items sequentially
   + Now batches items for parallel processing
   ```

4. **Behavioral Changes**:
   - Output format changes
   - Performance impact
   - Breaking changes
   - Bug fixes and improvements

5. **Risk Assessment**:
   - Compatibility impact on consumers
   - Security implications
   - Performance changes
   - Data quality effects

6. **Migration Guide**:
   - Required changes for upgrade
   - Update procedures
   - Rollback plan
   - Testing checklist

## Comparison Strategies

### Git Diff Strategy
```bash
# Compare two commits
git diff <commit1> <commit2> -- <file>

# Compare branches
git diff main..develop -- <file>

# Show stats
git diff --stat <commit1> <commit2>
```

### File Comparison Strategy
```bash
# Side-by-side diff
diff -u file_v1 file_v2

# Context diff
diff -c file_v1 file_v2

# Detailed diff with line numbers
grep -n . file_v1 > /tmp/v1.txt
grep -n . file_v2 > /tmp/v2.txt
diff /tmp/v1.txt /tmp/v2.txt
```

## Output Format

```
Version Comparison Report
========================

📊 Overview
-----------
Version 1: v1.2.3
Version 2: v1.3.0
Release Type: Minor (new features, backward compatible)

📈 Statistics
-------------
Files Changed: 12
Lines Added: 450
Lines Deleted: 120
Net Change: +330 lines

🔄 Structural Changes
---------------------
NEW Functions:
  - process_batch(items) — Process multiple items at once
  - validate_schema(data) — Schema validation helper
  - get_cached_result(key) — Caching support

REMOVED Functions:
  - legacy_process(item) — Deprecated in v1.2.5
  - old_validation(data) — Replaced by validate_schema()

MODIFIED Functions:
  - transform_data(input):
    * Added optional 'format' parameter
    * Changed return type from dict to object
    * Performance: 40% faster with new algorithm

📋 API Changes
--------------
Breaking Changes (requires code updates):
  - transform_data() now returns object instead of dict
  - get_user() removed (use get_user_by_id instead)

Non-Breaking Changes:
  - Added optional 'cache' parameter to all query functions
  - New export_data() function for bulk operations

⚡ Performance Impact
--------------------
Function: transform_data()
  Before: 450ms for 1000 items
  After: 270ms for 1000 items
  Improvement: 40% faster

Function: validate_schema()
  Before: N/A (new function)
  After: 12ms with caching
  Impact: Reduces validation overhead

🔐 Security Changes
-------------------
✅ Fixed: SQL injection vulnerability in query builder
✅ Improved: Password hashing algorithm (SHA256 → bcrypt)
⚠️ Note: Must update client code to use new auth method

🐛 Bug Fixes
------------
1. Fixed race condition in database write
   File: core/database.py:125
   Impact: Eliminates data corruption in concurrent scenarios

2. Fixed memory leak in cache manager
   File: lib/cache.py:89
   Impact: Reduces memory usage by 30%

🚀 New Features
---------------
1. Batch processing support
   API: process_batch(items)
   Use case: Processing multiple records at once

2. Result caching
   Method: enable_cache()
   Performance improvement: 50% faster repeated queries

🔄 Migration Guide
------------------
Step 1: Update imports
```python
# Old
from module import legacy_process
# New
from module import process_batch
```

Step 2: Update function calls
```python
# Old
for item in items:
    result = legacy_process(item)
# New
result = process_batch(items)
```

Step 3: Update authentication
```python
# Old
user = get_user(username)
# New
user = get_user_by_id(user_id)
```

Step 4: Test thoroughly
- Run full test suite
- Verify API responses match expectations
- Load test with production-like data

Rollback Plan:
- Revert to v1.2.3 if critical issues found
- Data is backward compatible
- No database migrations required

🎯 Recommendations
------------------
1. Update immediately: Security fixes are critical
2. Review before updating: Breaking API changes require code updates
3. Test thoroughly: New caching may affect behavior
4. Monitor: Performance improvements should reduce load

⚠️ Known Issues
---------------
- Batch processing not yet optimized for <100 items
- Cache invalidation timing changed (monitor consistency)
```

## Comparison Examples

### Example 1: Commit Range Comparison
User: `/compare-versions abc1234 def5678`

Response:
```
Comparing commits abc1234 to def5678...

I'll analyze structural and behavioral changes across all modified files.
```

### Example 2: Release Comparison
User: `/compare-versions v1.2.0 v1.3.0`

Response:
```
Comparing release versions v1.2.0 and v1.3.0...

I'll identify all breaking changes, new features, and migration requirements.
```

### Example 3: Branch Comparison
User: `/compare-versions main develop`

Response:
```
Comparing main and develop branches...

I'll identify what features are in develop but not yet in production.
```

## Key Metrics to Report

- **Lines changed** — Additions and deletions
- **Files affected** — How many files were modified
- **Breaking changes** — API incompatibilities
- **Performance delta** — Speed and memory changes
- **Security impact** — New vulnerabilities or fixes
- **Test coverage** — Before and after metrics

Remember: Clear version comparisons help teams make confident upgrade decisions.
