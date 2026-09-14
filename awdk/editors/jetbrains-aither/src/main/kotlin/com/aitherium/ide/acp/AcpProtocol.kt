package com.aitherium.ide.acp

import org.json.JSONArray
import org.json.JSONObject

/**
 * ACP (Agent Client Protocol) v2 wire helpers — stdio JSON-RPC 2.0.
 *
 * Mirrors the server implemented by awdk (`adk/adk/acp_server.py`), which
 * advertises exactly these methods over newline-delimited JSON-RPC 2.0:
 * `session/new|list|resume|load|close|delete|prompt|cancel`, with the server
 * pushing `session/update` notifications and `session/request_permission`
 * requests for the human-in-the-loop gate.
 */
object AcpProtocol {

    fun request(id: Int, method: String, params: JSONObject = JSONObject()): JSONObject =
        JSONObject()
            .put("jsonrpc", "2.0")
            .put("id", id)
            .put("method", method)
            .put("params", params)

    fun notification(method: String, params: JSONObject = JSONObject()): JSONObject =
        JSONObject()
            .put("jsonrpc", "2.0")
            .put("method", method)
            .put("params", params)

    // ------------------------------------------------------------------ events

    sealed class Event {
        /** A `session/update` notification. [message] is the raw update object. */
        data class Update(val sessionId: String, val update: JSONObject) : Event()

        /** A `session/request_permission` request — needs a resume() decision. */
        data class PermissionRequest(val requestId: Int, val params: JSONObject) : Event()

        /** An error notification or unparseable line. */
        data class Failure(val detail: String) : Event()
    }

    /** Convenience: the `state` field of an update ("running" | "requires_action" | "idle"). */
    fun stateOf(update: JSONObject): String? =
        update.optString("state").takeIf { it.isNotEmpty() }
            ?: update.optJSONObject("state_update")?.optString("state")

    /** The agent message text of an update, if present. */
    fun textOf(update: JSONObject): String? {
        val msg = update.optJSONObject("message") ?: return null
        val parts = msg.optJSONArray("parts") ?: return null
        val sb = StringBuilder()
        for (i in 0 until parts.length()) {
            val p = parts.optJSONObject(i) ?: continue
            if (p.optString("kind") == "text") sb.append(p.optString("text"))
        }
        return sb.toString().ifEmpty { null }
    }

    /** The decision options of a `requires_action` update. */
    fun optionsOf(update: JSONObject): JSONArray =
        update.optJSONArray("options") ?: JSONArray()

    fun promptParam(sessionId: String, text: String): JSONObject =
        JSONObject()
            .put("sessionId", sessionId)
            .put("prompt", JSONObject().put("parts", JSONArray().put(JSONObject().put("kind", "text").put("text", text))))

    fun resumeParam(sessionId: String, decisions: JSONArray): JSONObject =
        JSONObject().put("sessionId", sessionId).put("decisions", decisions)

    fun decision(optionId: String, selected: Boolean): JSONObject =
        JSONObject().put("optionId", optionId).put("selected", selected)
}
