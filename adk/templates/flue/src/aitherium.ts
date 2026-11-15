/**
 * The platform wiring for a Flue agent on Aitherium: which model answers, and
 * which tool server it reaches with whose sign-in. Flue supplies the harness;
 * everything in this file is a URL or a credential the platform already issues.
 *
 * Flue (Apache-2.0, https://github.com/withastro/flue) is used as a published
 * dependency. Nothing here is copied from its source.
 */
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createProvider } from '@earendil-works/pi-ai';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';
import { defineMcpConnection, setProvider } from '@flue/runtime';

const env = process.env;

/** OpenAI-compatible endpoint of the local model router (HTTPS, platform CA). */
export const LLM_BASE_URL = env.AITHER_LLM_BASE_URL ?? 'https://127.0.0.1:8150/v1';
/** A model that endpoint serves; `GET <base>/models` lists them. */
export const MODEL_ID = env.AITHER_MODEL ?? 'bonsai2-27b-5090';
/** Flue model specifier: `<provider id>/<model id>`. */
export const MODEL = `aitherium/${MODEL_ID}`;

/** MCP gateway: local by default, `https://mcp.aitherium.com/mcp` when hosted. */
export const MCP_URL = env.AITHER_MCP_URL ?? 'http://localhost:8182/mcp';

/**
 * Gateway tools this agent mounts. The gateway lists about 1,900 tools, which
 * is more schema than a local model's context holds, so the agent names the
 * few it needs. A name the gateway does not expose fails the run loudly.
 */
export const MCP_TOOLS = (env.AITHER_MCP_TOOLS ?? 'time_now,catalog_browse')
	.split(',')
	.map((name) => name.trim())
	.filter(Boolean);

/**
 * The person's own sign-in. `AITHER_MCP_TOKEN` wins; otherwise the session that
 * `adk login` wrote. Read on every request, so a renewed session is picked up
 * without a restart. A missing sign-in is an error, never an anonymous call.
 */
export function gatewayToken(): string {
	const fromEnv = env.AITHER_MCP_TOKEN?.trim();
	if (fromEnv) return fromEnv;
	const path = join(env.AITHER_HOME ?? join(homedir(), '.aither'), 'session-bearer');
	try {
		const token = readFileSync(path, 'utf8').trim();
		if (token) return token;
	} catch {
		// fall through to the error below
	}
	throw new Error(
		`No Aitherium sign-in found (looked at AITHER_MCP_TOKEN and ${path}). Run \`adk login\`.`,
	);
}

setProvider(
	createProvider({
		id: 'aitherium',
		// The local router needs no key, but the OpenAI client refuses to send a
		// request without one (measured: "No API key for provider" on pi-ai 1.1.0),
		// so a keyless endpoint gets a placeholder it ignores. A hosted endpoint
		// takes AITHER_LLM_API_KEY.
		auth: {
			apiKey: {
				name: 'Aitherium model endpoint',
				resolve: async () => ({ auth: { apiKey: env.AITHER_LLM_API_KEY || 'local' } }),
			},
		},
		models: [
			{
				id: MODEL_ID,
				name: `${MODEL_ID} (Aitherium)`,
				api: 'openai-completions',
				provider: 'aitherium',
				baseUrl: LLM_BASE_URL,
				reasoning: false,
				input: ['text'],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: Number(env.AITHER_MODEL_CONTEXT ?? 16384),
				maxTokens: Number(env.AITHER_MODEL_MAX_TOKENS ?? 2048),
			},
		],
		api: openAICompletionsApi(),
	}),
);

/** The gateway as one MCP server; its tools mount as `mcp__aitherium__<tool>`. */
export const aitherium = defineMcpConnection({
	name: 'aitherium',
	url: MCP_URL,
	auth: gatewayToken,
	tools: MCP_TOOLS,
});
