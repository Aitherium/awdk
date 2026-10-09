package com.aitherium.aither;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ComponentCallbacks2;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.ImageDecoder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.view.View;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;

/**
 * "What's in this picture?": take one or pick one (or share one to Aither from any app), then
 * ask. The model on this phone looks at it when it is downloaded and the phone can run it
 * (Vision.pick); otherwise, on the owner's tap, Aither online does, through the same chat as
 * the Aither tab. The first time, the owner chooses: download the picture model (once, its
 * size shown) so pictures stay on the phone, or ask online.
 */
public class DescribeActivity extends Activity {
    private static final int TAKE = 11, PICK = 12;
    private final Handler main = new Handler(Looper.getMainLooper());
    private Config cfg;
    private ImageView preview;
    private EditText question;
    private TextView status, answer;
    private TextView askHere, askOnline, download, notNow;
    /** The picture as it will be sent: JPEG, longest side at most Vision.MAX_SIDE. */
    private byte[] jpeg;
    private volatile boolean busy;
    private String lastAnswer = "";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        cfg = new Config(this);
        int pad = Ui.dp(this, 16);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad, pad, pad, pad);

        LinearLayout pick = Ui.card(this);
        pick.addView(Ui.action(this, "Take a picture", v -> take()));
        pick.addView(Ui.action(this, "Choose a picture", v -> choose()));
        col.addView(pick);

        preview = new ImageView(this);
        preview.setAdjustViewBounds(true);
        preview.setMaxHeight(Ui.dp(this, 320));
        preview.setVisibility(View.GONE);
        col.addView(preview);

        question = new EditText(this);
        question.setHint(Vision.DEFAULT_QUESTION);
        question.setTextColor(Ui.INK);
        col.addView(question);

        LinearLayout ask = Ui.card(this);
        askHere = Ui.action(this, "Ask on this phone", v -> run(true));
        askOnline = Ui.action(this, "Ask Aither online", v -> run(false));
        download = Ui.action(this, "Download the picture model (" + Vision.sizeMb() + ")", v -> fetch());
        notNow = Ui.action(this, "Not now", v -> {
            cfg.set("vision_declined", true);
            refresh();
        });
        ask.addView(askHere);
        ask.addView(download);
        ask.addView(askOnline);
        ask.addView(notNow);
        status = Ui.note(this, "");
        ask.addView(status);
        col.addView(ask);

        answer = Ui.text(this, "", 16, Ui.INK);
        answer.setPadding(0, pad, 0, pad);
        col.addView(answer);
        col.addView(Ui.action(this, "Report an answer", v -> Report.open(this, lastAnswer, "Picture")));

        ScrollView scroll = new ScrollView(this);
        scroll.addView(col);
        LinearLayout screen = new LinearLayout(this);
        screen.setOrientation(LinearLayout.VERTICAL);
        screen.setBackgroundColor(Ui.BG);
        LinearLayout bar = Ui.bar(this);
        bar.addView(Ui.icon(this, R.drawable.ic_nav_back, "Back", v -> finish()));
        TextView title = Ui.barTitle(this);
        title.setText("What's in this picture?");
        bar.addView(title);
        screen.addView(bar);
        screen.addView(Ui.rule(this));
        screen.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(screen);
        Edge.fit(screen);
        shared(getIntent());
        refresh();
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        setIntent(i);
        shared(i);
    }

    @Override
    public void onTrimMemory(int level) {
        super.onTrimMemory(level);
        if (level >= ComponentCallbacks2.TRIM_MEMORY_RUNNING_CRITICAL) {
            VisionEngine.unload("unloaded: the phone is low on memory");
        }
    }

    /** A picture shared to Aither from another app (ACTION_SEND image/*). Nothing is sent
     *  anywhere until the owner taps Ask. */
    private void shared(Intent i) {
        if (i == null || !Intent.ACTION_SEND.equals(i.getAction())) return;
        String type = i.getType();
        if (type == null || !type.startsWith("image/")) return;
        Uri u = Build.VERSION.SDK_INT >= 33
                ? i.getParcelableExtra(Intent.EXTRA_STREAM, Uri.class)
                : i.getParcelableExtra(Intent.EXTRA_STREAM);
        if (u != null) load(u);
    }

    private void take() {
        Intent i = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        ShotProvider.file(this).delete();
        i.putExtra(MediaStore.EXTRA_OUTPUT, ShotProvider.SHOT);
        i.setClipData(ClipData.newRawUri("shot", ShotProvider.SHOT));
        i.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            startActivityForResult(i, TAKE);
        } catch (ActivityNotFoundException e) {
            status.setText("This phone has no camera app to take a picture with.");
        }
    }

    private void choose() {
        Intent i = Build.VERSION.SDK_INT >= 33
                ? new Intent(MediaStore.ACTION_PICK_IMAGES) // the system photo picker: no permission
                : new Intent(Intent.ACTION_GET_CONTENT).setType("image/*").addCategory(Intent.CATEGORY_OPENABLE);
        try {
            startActivityForResult(i, PICK);
        } catch (ActivityNotFoundException e) {
            status.setText("This phone has no picture picker.");
        }
    }

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        super.onActivityResult(code, result, data);
        if (result != RESULT_OK) return;
        if (code == TAKE) load(Uri.fromFile(ShotProvider.file(this)));
        else if (code == PICK && data != null && data.getData() != null) load(data.getData());
    }

    /** Decode (rotated as taken), shrink to Vision.MAX_SIDE and keep it as JPEG. */
    private void load(Uri u) {
        status.setText("Reading the picture…");
        new Thread(() -> {
            try {
                ImageDecoder.Source src = "file".equals(u.getScheme())
                        ? ImageDecoder.createSource(new java.io.File(u.getPath()))
                        : ImageDecoder.createSource(getContentResolver(), u);
                Bitmap bm = ImageDecoder.decodeBitmap(src, (dec, info, s) -> {
                    int[] wh = Vision.scaled(info.getSize().getWidth(), info.getSize().getHeight(), Vision.MAX_SIDE);
                    dec.setTargetSize(wh[0], wh[1]);
                    dec.setAllocator(ImageDecoder.ALLOCATOR_SOFTWARE);
                });
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                bm.compress(Bitmap.CompressFormat.JPEG, 85, out);
                byte[] bytes = out.toByteArray();
                main.post(() -> {
                    if (isDestroyed()) return;
                    jpeg = bytes;
                    preview.setImageBitmap(bm);
                    preview.setVisibility(View.VISIBLE);
                    answer.setText("");
                    refresh();
                });
            } catch (Exception e) {
                main.post(() -> status.setText("That picture could not be read."));
            }
        }, "aither-picture").start();
    }

    /** Show the buttons the route allows, and say why. */
    private void refresh() {
        new Thread(() -> {
            Vision.Route r = VisionEngine.route(this, cfg);
            String why = VisionEngine.whyNotLocal(this, cfg);
            main.post(() -> {
                if (isDestroyed()) return;
                askHere.setVisibility(r == Vision.Route.LOCAL ? View.VISIBLE : View.GONE);
                download.setVisibility(r == Vision.Route.OFFER ? View.VISIBLE : View.GONE);
                notNow.setVisibility(r == Vision.Route.OFFER ? View.VISIBLE : View.GONE);
                boolean online = r != Vision.Route.LOCAL && r != Vision.Route.NONE
                        && Session.hasSession(android.webkit.CookieManager.getInstance().getCookie(AppTabs.ORIGIN));
                askOnline.setVisibility(online ? View.VISIBLE : View.GONE);
                boolean ready = jpeg != null && !busy;
                askHere.setEnabled(ready);
                askOnline.setEnabled(ready);
                download.setEnabled(!busy);
                if (busy) return;
                switch (r) {
                    case LOCAL:
                        status.setText("Looked at on this phone; the picture does not leave it.");
                        break;
                    case OFFER:
                        status.setText("Download the picture model once (" + Vision.sizeMb()
                                + ", SmolVLM, Apache-2.0) and pictures stay on this phone."
                                + (online ? " Or ask Aither online: the picture is sent to your account." : ""));
                        break;
                    case HOSTED:
                        status.setText("Not on this phone (" + why + "). Ask Aither online sends the"
                                + " picture to your account.");
                        break;
                    default:
                        status.setText("Not on this phone (" + why + "), and Aither online needs you"
                                + " signed in.");
                }
            });
        }, "aither-vision-route").start();
    }

    /** The owner tapped Download: fetch the picture model once, with its size and progress. */
    private void fetch() {
        if (busy) return;
        busy = true;
        cfg.set("vision_declined", false);
        download.setEnabled(false);
        status.setText("Downloading the picture model…");
        new Thread(() -> {
            String err = VisionEngine.download(this, (done, total) -> main.post(() ->
                    status.setText("Downloading the picture model: " + (done / 1_000_000) + " of " + (total / 1_000_000) + " MB")));
            main.post(() -> {
                busy = false;
                if (!err.isEmpty()) status.setText(err);
                refresh();
            });
        }, "aither-vision-download").start();
    }

    private void run(boolean here) {
        if (jpeg == null || busy) return;
        busy = true;
        askHere.setEnabled(false);
        askOnline.setEnabled(false);
        String q = question.getText().toString();
        byte[] pic = jpeg;
        status.setText(here ? "Looking at it on this phone…" : "Asking Aither online…");
        answer.setText("…");
        new Thread(() -> {
            String text;
            try {
                text = here ? VisionEngine.describe(this, cfg, pic, q) : VisionHosted.describe(pic, q);
                if (text.isEmpty()) text = "No answer came back.";
            } catch (Exception e) {
                text = "Sorry, that did not work (" + (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()) + ").";
            }
            String shown = text + (here ? "  [on this phone]" : "  [Aither online]");
            String asked = Vision.question(q);
            main.post(() -> {
                busy = false;
                lastAnswer = "Q: " + asked + "\nA: " + shown;
                answer.setText(shown);
                refresh();
            });
        }, "aither-describe").start();
    }
}
