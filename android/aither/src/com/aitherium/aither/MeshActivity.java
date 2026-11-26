package com.aitherium.aither;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;

/**
 * Native AitherMesh Voice & Posture screen on the phone: see every node on the owner's
 * Tailscale mesh (DGX Spark, PC 5090, OptiPlex), pick which node answers Ask Aither, switch
 * model postures in one tap, and pair via QR link (aither://mesh?hub=...&token=...).
 */
public class MeshActivity extends Activity {
    private MeshClient mesh;
    private final Handler main = new Handler(Looper.getMainLooper());
    private LinearLayout nodeList;
    private TextView nodeNote;
    private TextView targetNote;
    private TextView postureNote;
    private EditText hubInput;
    private EditText fallbackInput;
    private EditText tokenInput;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        mesh = new MeshClient(this);
        if (!mesh.allowed()) {
            Toast.makeText(this, "AitherMesh controls are only for adult devices.", Toast.LENGTH_LONG).show();
            finish();
            return;
        }
        handleUri(getIntent());

        LinearLayout screen = new LinearLayout(this);
        screen.setOrientation(LinearLayout.VERTICAL);
        screen.setBackgroundColor(Ui.BG);

        LinearLayout bar = Ui.bar(this);
        bar.addView(Ui.icon(this, R.drawable.ic_nav_back, "Back", v -> finish()));
        TextView title = Ui.barTitle(this);
        title.setText("AitherMesh nodes & voice");
        bar.addView(title);
        bar.addView(Ui.icon(this, R.drawable.ic_nav_refresh, "Refresh nodes", v -> refreshNodes()));
        screen.addView(bar);
        screen.addView(Ui.rule(this));

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(this, 16);
        col.setPadding(pad, pad / 2, pad, pad * 2);
        col.addView(Ui.note(this, "Route voice and model posture controls across your DGX Spark, 5090 PC, and OptiPlex over Tailscale."));

        // ---- Voice target
        col.addView(Ui.section(this, "Ask Aither voice target"));
        LinearLayout targetCard = Ui.card(this);
        targetNote = Ui.note(this, "Active target: " + MeshRemote.nodeLabel(mesh.target()));
        targetCard.addView(targetNote);
        String[][] targets = {
            {"auto", "Auto (best online mesh node)"},
            {"dgx-spark", "DGX Spark (GB10)"},
            {"pc", "PC 5090"},
            {"optiplex", "OptiPlex"},
            {"phone", "This phone (offline Bonsai 1.7B)"},
        };
        for (String[] t : targets) {
            targetCard.addView(Ui.action(this, t[1], v -> {
                mesh.setTarget(t[0]);
                targetNote.setText("Active target: " + MeshRemote.nodeLabel(mesh.target()));
                Toast.makeText(this, "Voice target: " + MeshRemote.nodeLabel(t[0]), Toast.LENGTH_SHORT).show();
            }));
        }
        targetCard.addView(Ui.action(this, "Open Ask Aither now", v ->
                startActivity(new Intent(this, AssistActivity.class))));
        col.addView(targetCard);

        // ---- Live mesh nodes
        col.addView(Ui.section(this, "Live AitherMesh nodes"));
        LinearLayout nodesCard = Ui.card(this);
        nodeNote = Ui.note(this, mesh.paired() ? "Checking mesh nodes…" : "Pair your mesh token below to probe Spark & PC.");
        nodesCard.addView(nodeNote);
        nodeList = new LinearLayout(this);
        nodeList.setOrientation(LinearLayout.VERTICAL);
        nodesCard.addView(nodeList);
        nodesCard.addView(Ui.action(this, "Refresh node status", v -> refreshNodes()));
        col.addView(nodesCard);

        // ---- Model posture switcher
        col.addView(Ui.section(this, "Switch model posture"));
        LinearLayout posCard = Ui.card(this);
        postureNote = Ui.note(this, "Applies to the selected mesh node (defaults to DGX Spark when set to Auto).");
        posCard.addView(postureNote);
        for (String[] p : MeshRemote.POSTURES) {
            posCard.addView(Ui.action(this, p[1] + " (" + p[0] + ")", v -> applyPosture(p[0])));
        }
        col.addView(posCard);

        // ---- Hub pairing
        col.addView(Ui.section(this, "Mesh hub pairing"));
        LinearLayout pairCard = Ui.card(this);
        pairCard.addView(Ui.note(this, "Press Win+Shift+V on DGX Spark to show a pairing QR, or paste the hub URL and token below."));
        hubInput = field("Primary hub (e.g. https://100.64.0.38:9448)", mesh.primaryHub(), false);
        fallbackInput = field("Fallback hub (e.g. https://100.64.0.46:9449)", mesh.fallbackHub(), false);
        tokenInput = field("Mesh bearer token (or paste full QR URL)", mesh.token(), true);
        pairCard.addView(hubInput);
        pairCard.addView(fallbackInput);
        pairCard.addView(tokenInput);
        pairCard.addView(Ui.action(this, "Save & test connection", v -> savePairing()));
        col.addView(pairCard);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(col);
        screen.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(screen);
        Edge.fit(screen);
        if (mesh.paired()) refreshNodes();
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        if (handleUri(i)) {
            if (hubInput != null) hubInput.setText(mesh.primaryHub());
            if (tokenInput != null) tokenInput.setText(mesh.token());
            if (targetNote != null) targetNote.setText("Active target: " + MeshRemote.nodeLabel(mesh.target()));
            refreshNodes();
        }
    }

    private boolean handleUri(Intent i) {
        Uri u = i == null ? null : i.getData();
        if (u == null) return false;
        if (mesh.acceptPairUri(u)) {
            Toast.makeText(this, "Paired AitherMesh hub: " + mesh.primaryHub(), Toast.LENGTH_LONG).show();
            return true;
        }
        return false;
    }

    private EditText field(String hint, String value, boolean secret) {
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setText(value);
        e.setTextColor(Ui.INK);
        e.setHintTextColor(Ui.FAINT);
        e.setTextSize(14);
        e.setSingleLine(true);
        e.setBackground(Ui.round(this, Ui.RAISED, Ui.RADIUS_SM, Ui.LINE));
        int p = Ui.dp(this, 10);
        e.setPadding(p, p, p, p);
        e.setInputType(secret
                ? (InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD)
                : (InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = Ui.dp(this, 8);
        e.setLayoutParams(lp);
        return e;
    }

    private void savePairing() {
        String tok = tokenInput.getText().toString().trim();
        // Allow pasting a full QR pairing URL directly into the token field
        if (tok.startsWith("https://") || tok.startsWith("aither://")) {
            MeshRemote.Pair pair = MeshRemote.parsePairUrl(tok);
            if (pair == null) {
                Toast.makeText(this, "Invalid AitherMesh pairing URL.", Toast.LENGTH_LONG).show();
                return;
            }
            hubInput.setText(pair.hub);
            tokenInput.setText(pair.token);
            tok = pair.token;
        }
        if (!mesh.saveConfig(hubInput.getText().toString(), fallbackInput.getText().toString(), tok)) {
            Toast.makeText(this, "Need a private Tailscale/LAN https:// hub URL and valid token.", Toast.LENGTH_LONG).show();
            return;
        }
        targetNote.setText("Active target: " + MeshRemote.nodeLabel(mesh.target()));
        Toast.makeText(this, "Saved. Testing AitherMesh…", Toast.LENGTH_SHORT).show();
        refreshNodes();
    }

    private void refreshNodes() {
        nodeNote.setText("Probing AitherMesh nodes…");
        nodeList.removeAllViews();
        new Thread(() -> {
            String[] err = {""};
            List<MeshClient.NodeInfo> nodes = mesh.fetchNodes(err);
            main.post(() -> {
                if (isDestroyed()) return;
                nodeList.removeAllViews();
                if (!err[0].isEmpty()) {
                    nodeNote.setText(err[0]);
                    return;
                }
                if (nodes.isEmpty()) {
                    nodeNote.setText("Connected, but no peers reported.");
                    return;
                }
                nodeNote.setText(nodes.size() + " mesh node(s) reported:");
                for (MeshClient.NodeInfo n : nodes) {
                    TextView row = Ui.text(this, n.summary(), 14, n.online ? Ui.INK : Ui.FAINT);
                    row.setPadding(0, Ui.dp(this, 6), 0, Ui.dp(this, 6));
                    row.setGravity(Gravity.START);
                    row.setOnClickListener(v -> {
                        if (MeshRemote.validNode(n.id)) {
                            mesh.setTarget(n.id);
                            targetNote.setText("Active target: " + MeshRemote.nodeLabel(mesh.target()));
                            Toast.makeText(this, "Target set to " + n.name, Toast.LENGTH_SHORT).show();
                        }
                    });
                    nodeList.addView(row);
                }
            });
        }, "aither-mesh-nodes").start();
    }

    private void applyPosture(String posture) {
        String target = mesh.target();
        String node = "phone".equals(target) || "auto".equals(target) ? "dgx-spark" : target;
        postureNote.setText("Applying " + posture + " on " + MeshRemote.nodeLabel(node) + "…");
        new Thread(() -> {
            MeshClient.PostureResult res = mesh.setPosture(posture, node);
            main.post(() -> {
                if (isDestroyed()) return;
                postureNote.setText((res.ok ? "Applied: " : "Failed: ") + res.detail);
                if (res.ok) refreshNodes();
            });
        }, "aither-mesh-posture").start();
    }
}
