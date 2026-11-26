package com.aitherium.aither;

import android.app.Activity;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * window.AitherMesh: lets AitherOS pages on aitherium.com inspect AitherMesh peers, switch
 * model postures, or open the native AitherMesh controls screen. Adult devices only.
 */
final class MeshBridge {
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private final Activity act;
    private final WebView web;

    MeshBridge(Activity a, WebView w) {
        act = a;
        web = w;
    }

    @JavascriptInterface
    public boolean available() {
        return new MeshClient(act).allowed();
    }

    @JavascriptInterface
    public boolean paired() {
        return new MeshClient(act).paired();
    }

    @JavascriptInterface
    public void openSettings() {
        if (!available()) return;
        MAIN.post(() -> {
            if (McpPolicy.ourPage(web.getUrl())) {
                act.startActivity(new Intent(act, MeshActivity.class));
            }
        });
    }

    @JavascriptInterface
    public void nodes(String id) {
        if (id == null || !available()) return;
        new Thread(() -> {
            if (!ourPage()) { deliver(id, false, "{\"error\":\"not an Aither page\"}"); return; }
            String[] err = {""};
            List<MeshClient.NodeInfo> list = new MeshClient(act).fetchNodes(err);
            try {
                JSONArray arr = new JSONArray();
                for (MeshClient.NodeInfo n : list) {
                    arr.put(new JSONObject().put("id", n.id).put("name", n.name)
                            .put("online", n.online).put("posture", n.posture)
                            .put("model", n.model).put("gpu", n.gpu).put("free_gib", n.freeGib));
                }
                JSONObject out = new JSONObject().put("nodes", arr).put("error", err[0]);
                deliver(id, err[0].isEmpty(), out.toString());
            } catch (Exception e) {
                deliver(id, false, "{\"error\":\"encode failed\"}");
            }
        }, "aither-mesh-bridge").start();
    }

    @JavascriptInterface
    public void setPosture(String id, String posture, String node) {
        if (id == null || !available()) return;
        new Thread(() -> {
            if (!ourPage()) { deliver(id, false, "{\"error\":\"not an Aither page\"}"); return; }
            MeshClient.PostureResult res = new MeshClient(act).setPosture(posture, node);
            try {
                JSONObject out = new JSONObject().put("ok", res.ok).put("posture", res.posture)
                        .put("node", res.node).put("detail", res.detail);
                deliver(id, res.ok, out.toString());
            } catch (Exception e) {
                deliver(id, false, "{\"error\":\"encode failed\"}");
            }
        }, "aither-mesh-posture-bridge").start();
    }

    private boolean ourPage() {
        final String[] url = {null};
        CountDownLatch done = new CountDownLatch(1);
        MAIN.post(() -> { url[0] = web.getUrl(); done.countDown(); });
        try {
            return done.await(2, TimeUnit.SECONDS) && McpPolicy.ourPage(url[0]);
        } catch (InterruptedException e) {
            return false;
        }
    }

    private void deliver(String id, boolean ok, String json) {
        String js = "window.__aitherMeshResult&&window.__aitherMeshResult("
                + JSONObject.quote(id) + "," + ok + "," + JSONObject.quote(json == null ? "{}" : json) + ")";
        MAIN.post(() -> web.evaluateJavascript(js, null));
    }
}
