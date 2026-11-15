# Flue agent on Aitherium

- The agent is `src/agents/aither.ts`: a `'use agent'` module exporting one capitalized function. Its returned string is the instruction.
- `src/aitherium.ts` registers the model provider and defines the MCP gateway connection. Change endpoints through environment variables (see `.env.example`), not by editing URLs into agent files.
- Gateway tools mount as `mcp__aitherium__<tool>`. Add one by naming it in `AITHER_MCP_TOOLS`.
- Run: `npx flue run src/agents/aither.ts -m "<message>"`. Typecheck: `npm run check:types`.
- Flue's full documentation is offline in the CLI: `npx flue docs search <query>`, `npx flue docs read <path>`.
- Never write a token into a file. The sign-in is read from `AITHER_MCP_TOKEN` or `~/.aither/session-bearer`.
