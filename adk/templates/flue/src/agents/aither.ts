'use agent';
import { useMcpConnection, useModel } from '@flue/runtime';
import { aitherium, MCP_TOOLS, MODEL } from '../aitherium.ts';

/**
 * A Flue agent that runs on Aitherium: a local model answers, and its tools are
 * the platform's, reached through the MCP gateway with your own sign-in.
 *
 *   npx flue run src/agents/aither.ts -m "What time is it on the platform?"
 */
export function Aither() {
	useModel(MODEL);
	useMcpConnection(aitherium);

	return `
You are an agent running on the Aitherium platform.
Your tools come from the platform's MCP gateway: ${MCP_TOOLS.map((t) => `mcp__aitherium__${t}`).join(', ')}.
When a question needs live platform data, call the tool and answer from its result.
Quote the values the tool returned. Never guess a value a tool can give you.`;
}
