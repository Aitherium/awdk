---
name: dependencies
allowed-tools: Bash, Read, Glob, WebSearch
description: Manage project dependencies, check for updates, and scan for vulnerabilities
argument-hint: [--update|--audit|--clean|--tree]
---

## Context
- Working directory: !`pwd`
- Command: $ARGUMENTS

## Your Role
You are a dependency management expert handling:
- Package dependency analysis
- Version conflict resolution
- Security vulnerability scanning
- License compliance checking
- Dependency optimization

## Your Task

1. **Parse Dependency Command**:
   - No args: Show dependency summary
   - --update: Update dependencies safely
   - --audit: Security vulnerability scan
   - --clean: Remove unused dependencies
   - --tree: Show dependency tree

2. **Dependency Analysis**:
   
   **Discovery Phase**:
   - Python: requirements.txt, Pipfile, pyproject.toml
   - JavaScript: package.json, yarn.lock
   - Docker: Dockerfile base images
   - System: OS packages
   
   **Analysis Phase**:
   - Version compatibility
   - Security vulnerabilities
   - License compliance
   - Update availability

3. **Execute Actions**:
   - Run pip-audit, npm audit, or equivalent for vulnerability checks
   - Analyze version updates (patch, minor, major)
   - Test updates before applying
   - Document changes

## Dependency Patterns

### Pattern 1: Security Audit
```
/dependencies --audit

Scanning dependencies for vulnerabilities...
- Python packages: 45 total, checking CVE database
- NPM packages: 123 total, checking npm audit
- Docker images: 3 total, checking image vulnerabilities
```

### Pattern 2: Smart Update
```
/dependencies --update

Analyzing safe update paths...
- Patch updates (1.2.3 → 1.2.4): Auto-apply
- Minor updates (1.2.0 → 1.3.0): Test first
- Major updates (1.0.0 → 2.0.0): Manual review
```

### Pattern 3: Dependency Tree
```
/dependencies --tree

Project Dependencies:
├── flask==2.3.2
│   ├── werkzeug>=2.3.3
│   ├── jinja2>=3.1.2
│   └── click>=8.1.3
├── requests==2.31.0
│   ├── urllib3>=1.26.5
│   └── certifi>=2022.12.7
```

## Output Format

```
Dependency Analysis Report
=========================

📦 Summary
----------
Total Dependencies: 89
Direct: 23
Transitive: 66
Outdated: 12
Vulnerable: 3

🚨 Security Vulnerabilities
--------------------------
Critical (1):
- pyyaml 5.3.1 → CVE-2020-14343 (RCE)
  Fix: Update to 6.0.1

High (2):
- flask 1.1.2 → CVE-2023-30861 (Cookie injection)
  Fix: Update to 2.3.2
- pillow 8.3.2 → CVE-2022-45199 (Buffer overflow)
  Fix: Update to 10.0.1

📊 Update Opportunities
----------------------
Package        Current   Latest    Type      Risk
requests       2.25.1    2.31.0    Minor     Low
pytest         6.2.5     7.4.3     Major     Medium
numpy          1.19.5    1.24.3    Major     High

📋 License Summary
-----------------
MIT: 45 packages
Apache-2.0: 23 packages
BSD: 15 packages
GPL-3.0: 2 packages ⚠️
Unknown: 4 packages ❓

🧹 Unused Dependencies
---------------------
Found 5 potentially unused:
- colorama (no imports found)
- six (Python 2 compatibility)
- mock (use unittest.mock)

💡 Recommendations
-----------------
1. Immediate: Update pyyaml (critical vulnerability)
2. This week: Update flask and pillow
3. Review: GPL licensed packages for compliance
4. Consider: Removing unused dependencies
```

## Dependency Management Tools

### Python Dependencies
```bash
# Check outdated packages
pip list --outdated

# Audit for vulnerabilities
pip-audit

# Alternative: Safety check
safety check

# Update safely
pip install --upgrade -r requirements.txt

# Pin transitive versions without editing requirements.txt
# (a constraints file only CAPS versions; it never installs anything itself)
pip install -r requirements.txt -c constraints.txt

# Generate lock file
pip freeze > requirements-lock.txt
```

> **A constraints file only works if something actually passes `-c`.** Copying one next
> to your requirements does nothing; neither does mentioning it in a comment. We found a
> `constraints.txt` in our own repo that was faithfully copied into every container image
> and then deleted at the end of the build — never once passed to `pip`. Its header
> promised that certain packages "must NEVER be downgraded", while the build on the line
> above deliberately installed the very thing it forbade. Grep for `-c` or `--constraint`
> in your Dockerfiles and CI before trusting one: an unenforced pin is worse than no pin,
> because everyone reads it and believes they are protected.

### Node.js Dependencies
```bash
# Audit for vulnerabilities
npm audit
yarn audit

# Update dependencies
npm update --save
npm audit fix

# Check for unused
npx depcheck

# Show dependency tree
npm ls
```

### Docker Dependencies
```dockerfile
# Pin base image versions
FROM python:3.11.4-slim-bullseye

# Use multi-stage builds
FROM python:3.11.4-slim-bullseye as builder
COPY requirements.txt .
RUN pip install --user -r requirements.txt

FROM python:3.11.4-slim-bullseye
COPY --from=builder /root/.local /root/.local
```

## Execution Steps

1. **Discover Dependencies**
   - Find all dependency manifest files (requirements.txt, package.json, Dockerfile, etc.)
   - Parse and catalog all direct and transitive dependencies

2. **Analyze for Vulnerabilities**
   - Run `pip-audit` for Python projects
   - Run `npm audit` for Node.js projects
   - Check against known CVE databases

3. **Assess Update Opportunities**
   - Categorize by patch/minor/major versions
   - Identify breaking changes
   - Check test coverage before applying updates

4. **License Compliance**
   - Extract licenses from each dependency
   - Flag incompatible licenses (GPL-3.0, AGPL, etc.)
   - Document for compliance review

5. **Clean Up**
   - Identify unused dependencies (static analysis)
   - Suggest removal candidates
   - Verify removal doesn't break anything

## Examples

### Example 1: Quick Status
User: `/dependencies`

Response:
```
Analyzing project dependencies...

I'll provide a summary of all dependencies and their status.
```

### Example 2: Security Focus
User: `/dependencies --audit`

Response:
```
Running security audit on all dependencies...

I'll check for known vulnerabilities and provide remediation steps.
```

### Example 3: Clean Install
User: `/dependencies --clean`

Response:
```
Identifying and removing unused dependencies...

I'll analyze imports and remove packages that aren't being used.
```

Remember: Keep dependencies updated but test thoroughly. Security updates should be applied immediately.
