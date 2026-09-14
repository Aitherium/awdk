package com.aitherium.ide.acp

import com.aitherium.ide.acp.AcpProtocol.Event
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.Closeable
import java.io.OutputStreamWriter
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit

/**
 * ACP v2 CLIENT — drives the awdk ACP server (`adk acp serve --agent <name>`)
 * over stdio JSON-RPC 2.0, with no editor-specific code, exactly as the server
 * documents. This is the counterpart to `adk/acp_server.py`'s `serve_stdio`.
 *
 * Lifecycle:
 *  - [start] spawns the server process and begins reading stdout.
 *  - [newSession] / [prompt] / [resume] / [cancel] / [close] drive one session.
 *  - Server-pushed `session/update` notifications are routed to [onEvent].
 *  - `session/request_permission` requests are surfaced as [Event.PermissionRequest];
 *    the caller answers with [resume].
 */
class AcpClient(
    private val adkCommand: List<String>,
    private val scope: CoroutineScope,
) : Closeable {

    @Volatile private var process: Process? = null
    private var writer: BufferedWriter? = null
    private var readerJob: Job? = null

    private val mutex = Mutex()
    private val pending = ConcurrentHashMap<Int, CompletableFuture<JSONObject>>()
    private val listeners = CopyOnWriteArrayList<(Event) -> Unit>()
    private val ids = java.util.concurrent.atomic.AtomicInteger(1)
    private val permissionIds = ConcurrentHashMap<Int, CompletableFuture<JSONArray>>()

    @Volatile var serverPid: Long = -1
        private set

    /** True while the underlying ACP server process is alive. */
    @Volatile var isRunning: Boolean = false
        private set

    @Volatile var lastError: String? = null
        private set

    fun onEvent(listener: (Event) -> Unit) {
        listeners += listener
    }

    /** Spawn `adk acp serve --agent <name>` and begin reading its stdout. */
    fun start(): AcpClient {
        if (process?.isAlive == true) return this
        val pb = ProcessBuilder(adkCommand)
            .redirectErrorStream(true)
        val p = try {
            pb.start()
        } catch (e: Exception) {
            lastError = "failed to start '${adkCommand.joinToString(" ")}': ${e.message}"
            isRunning = false
            return this
        }
        process = p
        serverPid = p.pid()
        isRunning = true
        writer = BufferedWriter(OutputStreamWriter(p.outputStream, Charsets.UTF_8))
        readerJob = scope.launch(Dispatchers.IO) { readLoop(p) }
        return this
    }

    private suspend fun readLoop(p: Process) {
        p.inputStream.bufferedReader(Charsets.UTF_8).useLines { lines ->
            for (line in lines) {
                if (line.isBlank()) continue
                val msg = try {
                    JSONObject(line)
                } catch (e: Exception) {
                    // The adk ACP server logs human-readable lines to stdout
                    // (identity resolution, license notices) in the SAME stream
                    // as its JSON-RPC responses — those are not protocol errors,
                    // so skip them rather than surfacing them as failures.
                    continue
                }
                if (msg.has("id")) {
                    // A response to one of our requests.
                    val id = msg.optInt("id")
                    pending.remove(id)?.complete(msg)
                } else {
                    // A notification or request from the server.
                    when (msg.optString("method")) {
                        "session/update" -> {
                            val params = msg.optJSONObject("params")
                            dispatch(Event.Update(params?.optString("sessionId") ?: "", params?.optJSONObject("update") ?: JSONObject()))
                        }
                        "session/request_permission" -> {
                            val id = msg.optInt("id")
                            val params = msg.optJSONObject("params")
                            // The server expects a response to this request id. Route to the
                            // caller first; it decides via resume(), which completes here.
                            scope.launch(Dispatchers.IO) {
                                val decisions = CompletableFuture<JSONArray>()
                                permissionIds[id] = decisions
                                dispatch(Event.PermissionRequest(id, params ?: JSONObject()))
                                val decisionArr = try {
                                    decisions.get(10, TimeUnit.MINUTES)
                                } catch (e: Exception) {
                                    JSONArray()
                                }
                                writeRaw(AcpProtocol.request(id, "session/resume", JSONObject().put("decisions", decisionArr)).toString())
                            }
                        }
                        "session/update.v2" -> { /* reserved */ }
                        else -> {
                            if (msg.has("error")) dispatch(Event.Failure(msg.optJSONObject("error")?.optString("message") ?: "server error"))
                        }
                    }
                }
            }
        }
        // Process exited.
        isRunning = false
        dispatch(Event.Failure("ACP server exited (pid $serverPid, exit ${p.exitValue()})"))
    }

    private fun dispatch(event: Event) {
        for (l in listeners) l(event)
    }

    private suspend fun writeRaw(line: String) {
        mutex.withLock {
            val w = writer ?: return
            w.write(line)
            w.write("\n")
            w.flush()
        }
    }

    private suspend fun call(method: String, params: JSONObject = JSONObject(), timeoutMs: Long = 120_000): JSONObject {
        val p = process
        if (p == null || !p.isAlive) {
            throw IllegalStateException(lastError ?: "ACP server is not running")
        }
        val id = ids.getAndIncrement()
        val future = CompletableFuture<JSONObject>()
        pending[id] = future
        writeRaw(AcpProtocol.request(id, method, params).toString())
        val resp = future.get(timeoutMs, TimeUnit.MILLISECONDS)
        val err = resp.optJSONObject("error")
        if (err != null) throw IllegalStateException("ACP $method failed: ${err.optString("message")} (${err.optInt("code")})")
        return resp.optJSONObject("result") ?: JSONObject()
    }

    // ------------------------------------------------------------ ACP methods

    /** ACP `initialize` handshake — call once after [start]. Returns capabilities. */
    suspend fun initialize(clientName: String = "aither-jetbrains", clientVersion: String = "0.1.0"): JSONObject =
        call("initialize", JSONObject()
            .put("protocolVersion", 2)
            .put("clientInfo", JSONObject().put("name", clientName).put("version", clientVersion)))

    suspend fun newSession(cwd: String? = null): String {
        val params = JSONObject()
        if (cwd != null) params.put("cwd", cwd)
        return call("session/new", params).optString("sessionId")
    }

    suspend fun prompt(sessionId: String, text: String): JSONObject =
        call("session/prompt", AcpProtocol.promptParam(sessionId, text))

    /** Answer a `session/request_permission` request with per-option allow/deny. */
    suspend fun resume(sessionId: String, decisions: JSONArray) {
        call("session/resume", AcpProtocol.resumeParam(sessionId, decisions))
    }

    suspend fun cancel(sessionId: String) {
        call("session/cancel", JSONObject().put("sessionId", sessionId))
    }

    suspend fun close(sessionId: String) {
        call("session/close", JSONObject().put("sessionId", sessionId))
    }

    suspend fun list(): JSONArray =
        call("session/list").optJSONArray("sessions") ?: JSONArray()

    // ----------------------------------------------------------------- teardown

    override fun close() {
        process?.let { p ->
            try { p.destroy() } catch (_: Exception) {}
            try { if (!p.waitFor(3, TimeUnit.SECONDS)) p.destroyForcibly() } catch (_: Exception) {}
        }
        readerJob?.cancel()
        process = null
        isRunning = false
    }
}
