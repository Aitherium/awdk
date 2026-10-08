package com.aitherium.aither;

import java.net.URI;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/**
 * Which of a page's WebMCP tools the phone's agent may use (PageTools asks; pure Java so
 * test/McpPolicyCheck.java runs it on a desktop JVM).
 *
 * Read-only by default: a tool that only reads the page or the site runs without asking.
 * A tool that writes, moves the page, makes sound or touches the clipboard, site storage or
 * downloads runs only after the person says yes to that one call. Any other tool a page
 * registers is not offered at all, so a tool added to a page later is refused until it is
 * named here. Only aitherium.com pages get the surface, and only the tools the page on
 * screen registered can be called.
 */
final class McpPolicy {
    enum Access { READ, CONFIRM, NONE }

    /** Read the page or the site, change nothing. */
    static final Set<String> READ = new HashSet<>(Arrays.asList(
            "get_site_overview", "search_site", "read_page",
            "page_read_dom", "page_read_selection",
            "web_search", "page_context", "get_thread"));

    /** Act for the person: each call is shown to them first and runs only on yes. */
    static final Set<String> CONFIRM = new HashSet<>(Arrays.asList(
            "page_fill_form", "page_storage_get", "page_download",
            "page_clipboard_read", "page_clipboard_write",
            "navigate_to", "speak", "deep_research"));

    /** The app's own tools (CalendarTool.NAME): a page cannot register over them. */
    static final Set<String> NATIVE = new HashSet<>(Arrays.asList("calendar_read"));

    /** Most page tools the agent is offered in one turn (a 1.7B's prompt is small). */
    static final int MAX_TOOLS = 8;
    static final int MAX_DESCRIPTION = 240;
    /** Longest page-tool result handed to the model. */
    static final int MAX_RESULT = 3000;
    /** Longest argument text shown in the confirm dialog. */
    static final int MAX_SHOWN_ARGS = 400;

    private McpPolicy() {}

    static Access access(String name) {
        if (!validName(name)) return Access.NONE;
        if (READ.contains(name)) return Access.READ;
        if (CONFIRM.contains(name)) return Access.CONFIRM;
        return Access.NONE;
    }

    /** May the agent call `name`: the page on screen registered it and it is allowed. */
    static boolean callable(String name, Set<String> registered, String pageUrl) {
        return ourPage(pageUrl) && registered != null && registered.contains(name)
                && access(name) != Access.NONE && !NATIVE.contains(name);
    }

    static boolean validName(String name) {
        return name != null && name.matches("[a-z][a-z0-9_]{0,63}");
    }

    /** An https page on aitherium.com or a subdomain, default port, no user info. */
    static boolean ourPage(String url) {
        if (url == null || url.isEmpty()) return false;
        try {
            URI u = new URI(url);
            String h = u.getHost();
            return "https".equals(u.getScheme()) && h != null && u.getRawUserInfo() == null
                    && (u.getPort() == -1 || u.getPort() == 443)
                    && (h.equals("aitherium.com") || h.endsWith(".aitherium.com"));
        } catch (Exception e) {
            return false;
        }
    }

    static String clip(String s, int max) {
        if (s == null) return "";
        return s.length() <= max ? s : s.substring(0, max);
    }

    /** A tool's result as the model sees it: clipped, and announced as cut when it was. */
    static String result(String s) {
        if (s == null || s.isEmpty()) return "(the tool returned nothing)";
        if (s.length() <= MAX_RESULT) return s;
        return s.substring(0, MAX_RESULT) + "\n[cut off after " + MAX_RESULT + " characters]";
    }
}
