package com.aitherium.ide.ui

import com.aitherium.ide.a2a.A2aClient
import com.aitherium.ide.acp.AcpClient
import com.aitherium.ide.acp.AcpProtocol
import com.intellij.openapi.project.Project
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.json.JSONArray
import java.awt.BorderLayout
import java.awt.Dimension
import java.awt.FlowLayout
import javax.swing.JButton
import javax.swing.JComboBox
import javax.swing.JLabel
import javax.swing.JPanel
import javax.swing.JTextField
import javax.swing.SwingUtilities
import javax.swing.border.EmptyBorder

/**
 * The "Aither Agent" panel: drives awdk agents from the IDE.
 *
 * Two modes:
 *  - **ACP** — spawns `adk acp serve --agent <name>` and chats with that agent
 *    over the Agent Client Protocol (stdio JSON-RPC v2).
 *  - **A2A** — sends a signed message to a remote agent (by mesh name or a raw
 *    invoke URL) and shows the reply.
 */
class ChatPanel(private val project: Project) : JPanel(BorderLayout()) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var acpJob: Job? = null

    private var acp: AcpClient? = null
    private var sessionId: String? = null

    private val log = JBTextArea().apply {
        isEditable = false
        lineWrap = true
        wrapStyleWord = true
        rows = 18
    }

    private val agentName = JTextField("atlas", 14)
    private val a2aTarget = JTextField(18)
    private val input = JTextField(28)
    private val mode = JComboBox(arrayOf("ACP — local agent (adk acp serve)", "A2A — message a remote agent"))
    private val connect = JButton("Connect")
    private val send = JButton("Send")

    init {
        border = EmptyBorder(8, 8, 8, 8)

        // ---- top controls
        val top = JPanel(FlowLayout(FlowLayout.LEFT, 6, 0)).apply {
            add(JLabel("Agent:"))
            add(agentName)
            add(connect)
            add(JLabel("Mode:"))
            add(mode)
        }
        val a2aRow = JPanel(FlowLayout(FlowLayout.LEFT, 6, 0)).apply {
            add(JLabel("A2A target (mesh name or http://host:port):"))
            add(a2aTarget)
        }
        val controls = JPanel(BorderLayout()).apply {
            add(top, BorderLayout.NORTH)
            add(a2aRow, BorderLayout.CENTER)
        }
        add(controls, BorderLayout.NORTH)

        // ---- log
        val scroll = JBScrollPane(log).apply { preferredSize = Dimension(360, 360) }
        add(scroll, BorderLayout.CENTER)

        // ---- input row
        val bottom = JPanel(FlowLayout(FlowLayout.LEFT, 6, 0)).apply {
            add(input)
            add(send)
        }
        add(bottom, BorderLayout.SOUTH)

        append("Aither — ACP + A2A integration. Agent = 'adk acp serve --agent <name>'. Type agent name, hit Connect, then chat.\n")

        connect.addActionListener { onConnect() }
        send.addActionListener { onSend() }
        input.addActionListener { onSend() }
    }

    // ------------------------------------------------------------------ ACP

    private fun onConnect() {
        val name = agentName.text.trim()
        if (name.isEmpty()) { append("Agent name required.\n"); return }
        connect.isEnabled = false
        scope.launch(Dispatchers.IO) {
            acp?.close()
            val client = AcpClient(listOf("adk", "acp", "serve", "--name", name), scope)
            client.onEvent { event -> dispatchEvent(name, event) }
            client.start()
            acp = client
            if (!client.isRunning) {
                append("ACP server failed to start: ${client.lastError ?: "unknown"}\n")
                SwingUtilities.invokeLater { connect.isEnabled = true }
                return@launch
            }
            append("ACP server up (pid ${client.serverPid}). Handshake…\n")
            try { client.initialize() } catch (e: Exception) { append("! initialize failed: ${e.message}\n") }
            append("Creating session…\n")
            val sid = try { client.newSession(project.basePath) } catch (e: Exception) { null }
            if (sid == null) {
                append("session/new failed: ${client.lastError}\n")
                SwingUtilities.invokeLater { connect.isEnabled = true }
                return@launch
            }
            sessionId = sid
            append("Session $sid ready. Type below.\n")
            SwingUtilities.invokeLater { connect.isEnabled = true }
        }
    }

    private fun dispatchEvent(agent: String, event: AcpProtocol.Event) {
        when (event) {
            is AcpProtocol.Event.Update -> {
                val state = AcpProtocol.stateOf(event.update)
                val text = AcpProtocol.textOf(event.update)
                if (!text.isNullOrBlank()) append("[${event.sessionId.take(8)}] $text\n")
                when (state) {
                    "requires_action" -> {
                        val opts = AcpProtocol.optionsOf(event.update)
                        append("[gate] agent needs approval for ${opts.length()} pending tool call(s). Reply 'y' to allow all, 'n' to deny all.\n")
                    }
                    "idle" -> append("[${event.sessionId.take(8)}] done.\n")
                }
            }
            is AcpProtocol.Event.PermissionRequest -> {
                // Answered via the pending-input path in onSend: on 'y'/'n' we resume.
                lastPermissionRequest = event
                append("[gate] request #${event.requestId}: ${event.params.optJSONArray("options")?.length() ?: 0} option(s). Reply 'y' (allow all) or 'n' (deny all).\n")
            }
            is AcpProtocol.Event.Failure -> append("! ${event.detail}\n")
        }
    }

    private var lastPermissionRequest: AcpProtocol.Event.PermissionRequest? = null

    private fun onSend() {
        val text = input.text.trim()
        if (text.isEmpty()) return
        input.text = ""
        append("> $text\n")
        scope.launch(Dispatchers.IO) {
            when (mode.selectedIndex) {
                0 -> acpSend(text)
                else -> a2aSend(text)
            }
        }
    }

    private suspend fun acpSend(text: String) {
        val c = acp ?: run { append("Not connected. Hit Connect first.\n"); return }
        val sid = sessionId ?: run { append("No session. Hit Connect first.\n"); return }
        val t = text.trim().lowercase()
        if (lastPermissionRequest != null && (t == "y" || t == "n")) {
            val req = lastPermissionRequest!!
            val opts = req.params.optJSONArray("options") ?: JSONArray()
            val decisions = JSONArray()
            for (i in 0 until opts.length()) {
                val o = opts.optJSONObject(i) ?: continue
                decisions.put(AcpProtocol.decision(o.optString("optionId"), t == "y"))
            }
            lastPermissionRequest = null
            try { c.resume(sid, decisions) } catch (e: Exception) { append("! resume failed: ${e.message}\n") }
            return
        }
        try {
            c.prompt(sid, text)
        } catch (e: Exception) {
            append("! prompt failed: ${e.message}\n")
        }
    }

    // ------------------------------------------------------------------- A2A

    private suspend fun a2aSend(text: String) {
        val target = a2aTarget.text.trim()
        if (target.isEmpty()) { append("A2A target required (agent name or http://host:port).\n"); return }
        val client = A2aClient()
        val result = if (target.startsWith("http://") || target.startsWith("https://")) {
            client.sendMessage(target, text, thisAgentName = "jetbrains")
        } else {
            // Resolve by name: default mesh endpoints live under awdk's
            // registry; if none is configured the caller can paste a URL.
            client.sendMessageByName({ name -> resolveAgentUrl(name) }, target, text, thisAgentName = "jetbrains")
        }
        if (result.ok) {
            append("[a2a → $target] ${result.payload?.optJSONObject("message")?.optString("text") ?: result.payload?.toString().orEmpty().take(400)}\n")
        } else {
            append("! [a2a → $target] ${result.error}\n")
        }
    }

    /** Resolve a mesh agent name to an invoke URL, if awdk's registry is known. */
    private fun resolveAgentUrl(name: String): String? {
        // Best-effort: the awdk mesh registers agents as
        // <name>-agent / <name> hostnames on the AitherNet overlay. Without a
        // direct registry read we fall back to the conventional mesh hostnames.
        return null // caller pastes an explicit URL, or a later registry reader fills this
    }

    // ------------------------------------------------------------------ misc

    private fun append(line: String) {
        SwingUtilities.invokeLater {
            log.append(line)
            log.caretPosition = log.document.length
        }
    }
}
