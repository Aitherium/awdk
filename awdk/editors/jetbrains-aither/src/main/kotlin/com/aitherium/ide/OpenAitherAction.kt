package com.aitherium.ide

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.wm.ToolWindowManager

/** Opens the "Aither Agent" tool window (Tools menu -> Open Aither Agent). */
class OpenAitherAction : AnAction() {
    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val toolWindow = ToolWindowManager.getInstance(project).getToolWindow("Aither Agent") ?: return
        toolWindow.activate(null)
    }
}
