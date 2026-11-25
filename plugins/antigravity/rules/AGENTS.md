# Aither World & awdk Rules (Google Antigravity)

- **Platform first — never hand-roll.** Use existing `aw*` lanes (`awrun`, `awgit`, `awsettings`, `awkno`, `awrelay`, `awm`, `awsh`, `awfocus`, `awdecide`, `awask`, or MCP tools) before writing ad-hoc scripts or starting bare background processes.
- **Concurrent-safe git (`awgit`).** In shared repositories, acquire path leases (`awgit lease acquire <paths>`) and commit scoped paths (`awgit blob-commit`). Never run raw `git merge`, `git rebase`, `git pull`, `git stash`, `git clean`, bare `git reset`, or pathspec-less `git commit`.
- **Two-way delegation.** Use `awdk-local` (`awdelegate`) and `awdk-awsh` (`awsh`) MCP tools or the `aw` subagent to offload sub-tasks to local models (DeepSeek/vLLM/MicroScheduler) or peer harnesses (`awdk`, `codex`, `opencode`, `gemini`, `aither`).
- **Secrets.** Never echo tokens, API keys, or credentials into prose, logs, or commits.
