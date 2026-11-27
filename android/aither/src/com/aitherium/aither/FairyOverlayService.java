package com.aitherium.aither;

import android.animation.ValueAnimator;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RadialGradient;
import android.graphics.Rect;
import android.graphics.Shader;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.text.TextUtils;
import android.util.DisplayMetrics;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.HapticFeedbackConstants;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.WindowMetrics;
import android.view.animation.DecelerateInterpolator;
import android.webkit.CookieManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * The Aither spirit orb as a SYSTEM overlay: floats cleanly docked to the screen edge
 * over other apps, snaps smoothly on drag, auto-hides inside the Aither app itself,
 * dims when idle, supports drag-to-dismiss at bottom-center, and — on tap — opens a
 * sleek floating HUD panel right over whatever app you are in showing live open
 * decisions (with 1-tap answer buttons) and active Antigravity / fleet sessions (with
 * inline steer replies), without kicking you out of your current app.
 *
 * Pure Android Views + Canvas (no Gradle/AndroidX, no third-party deps).
 */
public class FairyOverlayService extends Service {
    public static final String ACTION_SHOW = "com.aitherium.aither.FAIRY_SHOW";
    public static final String ACTION_HIDE = "com.aitherium.aither.FAIRY_HIDE";
    private static final String CHANNEL = "fairy-overlay";
    private static final int NOTICE_ID = 41;
    private static final String PREFS = "fairy_overlay";
    private static final String KEY_DOCK_RIGHT = "dock_right";
    private static final String KEY_Y_FRACTION = "y_fraction";

    private static final long IDLE_DIM_MS = 3500L;
    private static final float IDLE_ALPHA = 0.58f;

    /** Tracks whether any in-app UI activity is currently in the foreground so we auto-hide inside Aither. */
    private static volatile boolean appInForeground = false;
    private static volatile FairyOverlayService activeInstance = null;

    /** Called by AitherApp.ActivityLifecycleCallbacks so the overlay hides cleanly inside Aither. */
    public static void notifyAppForeground(boolean inForeground) {
        appInForeground = inForeground;
        FairyOverlayService svc = activeInstance;
        if (svc != null && svc.ui != null) {
            svc.ui.post(svc::syncAppVisibility);
        }
    }

    private WindowManager wm;
    private FairyOrbView orbView;
    private WindowManager.LayoutParams orbLp;
    private DismissTargetView dismissView;
    private WindowManager.LayoutParams dismissLp;
    private FrameLayout panelRoot;
    private LinearLayout panelSheet;
    private LinearLayout panelContent;
    private TextView panelSubtitle;
    private TextView panelStatusLine;
    private WindowManager.LayoutParams panelLp;
    private boolean panelOpen = false;
    private String expandedSessionId = null;
    private ValueAnimator snapAnim;

    private volatile FairyOverlayState state = FairyOverlayState.empty();
    private volatile boolean signedInNow = false;
    private Handler ui;
    private ScheduledExecutorService feed;
    private ConnectivityManager.NetworkCallback net;
    private boolean dragging = false;
    private boolean hoveringDismiss = false;
    private boolean animScheduled = false;
    private float density = 1f;
    private int orbSizePx;
    private int dismissSizePx;

    private final Runnable idleDimTask = this::applyIdleAlpha;
    private final Runnable animFrameTask = new Runnable() {
        @Override
        public void run() {
            animScheduled = false;
            if (orbView == null || orbView.getVisibility() != View.VISIBLE) return;
            orbView.invalidate();
            scheduleNextAnimFrame();
        }
    };

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_HIDE.equals(action)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTICE_ID, notification());
        if (orbView == null) {
            ui = new Handler(Looper.getMainLooper());
            density = getResources().getDisplayMetrics().density;
            orbSizePx = dp(56);
            dismissSizePx = dp(60);
            attach();
            startFeed();
        }
        activeInstance = this;
        appInForeground = AitherApp.isForeground();
        syncAppVisibility();
        return START_STICKY;
    }

    private int dp(float v) {
        return Math.max(1, Math.round(v * density));
    }

    private Notification notification() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "Floating Aither orb", NotificationManager.IMPORTANCE_LOW);
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(ch);
        }
        Intent open = new Intent(this, MainActivity.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        return new Notification.Builder(this, CHANNEL)
                .setContentTitle("Aither orb active")
                .setContentText("Tap the edge orb for live Antigravity sessions & decisions, or drag to × to hide.")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(PendingIntent.getActivity(this, 0, open, flags))
                .build();
    }

    private static final class SafeBounds {
        final int width;
        final int height;
        final int left;
        final int top;
        final int right;
        final int bottom;

        SafeBounds(int width, int height, int left, int top, int right, int bottom) {
            this.width = Math.max(1, width);
            this.height = Math.max(1, height);
            this.left = Math.max(0, left);
            this.top = Math.max(0, top);
            this.right = Math.max(0, right);
            this.bottom = Math.max(0, bottom);
        }
    }

    private SafeBounds safeBounds() {
        if (Build.VERSION.SDK_INT >= 30 && wm != null) {
            WindowMetrics wmMetrics = wm.getCurrentWindowMetrics();
            Rect b = wmMetrics.getBounds();
            Insets i = wmMetrics.getWindowInsets().getInsetsIgnoringVisibility(
                    WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            return new SafeBounds(b.width(), b.height(), i.left, i.top, i.right, i.bottom);
        }
        return safeBoundsLegacy();
    }

    @SuppressWarnings("deprecation")
    private SafeBounds safeBoundsLegacy() {
        DisplayMetrics dm = new DisplayMetrics();
        if (wm != null && wm.getDefaultDisplay() != null) {
            wm.getDefaultDisplay().getRealMetrics(dm);
        } else {
            dm = getResources().getDisplayMetrics();
        }
        int statusBar = dimenPx("status_bar_height", dp(28));
        int navBar = dimenPx("navigation_bar_height", dp(48));
        return new SafeBounds(dm.widthPixels, dm.heightPixels, dp(4), statusBar, dp(4), navBar);
    }

    private int dimenPx(String name, int fallback) {
        int id = getResources().getIdentifier(name, "dimen", "android");
        if (id <= 0) return fallback;
        int px = getResources().getDimensionPixelSize(id);
        return px > 0 ? px : fallback;
    }

    private int clampY(int y, SafeBounds sb) {
        int minY = sb.top + dp(8);
        int maxY = Math.max(minY, sb.height - sb.bottom - orbSizePx - dp(8));
        return Math.max(minY, Math.min(maxY, y));
    }

    private int edgeX(boolean dockRight, SafeBounds sb) {
        int margin = dp(4);
        int leftX = sb.left + margin;
        int rightX = Math.max(leftX, sb.width - sb.right - orbSizePx - margin);
        return dockRight ? rightX : leftX;
    }

    private void attach() {
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        boolean dockRight = prefs.getBoolean(KEY_DOCK_RIGHT, true);
        float yFrac = prefs.getFloat(KEY_Y_FRACTION, 0.36f);
        SafeBounds sb = safeBounds();

        // Bottom-center drag-to-dismiss target (hidden until dragging begins).
        dismissView = new DismissTargetView(this);
        dismissView.setVisibility(View.GONE);
        dismissLp = new WindowManager.LayoutParams(
                dismissSizePx, dismissSizePx,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        dismissLp.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
        dismissLp.layoutInDisplayCutoutMode =
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        if (Build.VERSION.SDK_INT >= 30) dismissLp.setFitInsetsTypes(0);
        dismissLp.x = (sb.left - sb.right) / 2;
        dismissLp.y = sb.bottom + dp(16);
        wm.addView(dismissView, dismissLp);

        // Luminous Aither Spirit Orb docked neatly at the screen edge.
        orbView = new FairyOrbView(this);
        orbLp = new WindowManager.LayoutParams(
                orbSizePx, orbSizePx,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        orbLp.gravity = Gravity.TOP | Gravity.START;
        orbLp.layoutInDisplayCutoutMode =
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        if (Build.VERSION.SDK_INT >= 30) orbLp.setFitInsetsTypes(0);
        orbLp.x = edgeX(dockRight, sb);
        int minY = sb.top + dp(8);
        int maxY = Math.max(minY, sb.height - sb.bottom - orbSizePx - dp(8));
        orbLp.y = clampY(minY + Math.round((maxY - minY) * yFrac), sb);

        final int[] start = new int[2];
        final float[] touch = new float[2];
        final float dragThreshold = dp(8);

        orbView.setOnTouchListener((view, e) -> {
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    cancelSnap();
                    wakeAlpha();
                    start[0] = orbLp.x;
                    start[1] = orbLp.y;
                    touch[0] = e.getRawX();
                    touch[1] = e.getRawY();
                    dragging = false;
                    hoveringDismiss = false;
                    orbView.setPressedGlow(true);
                    return true;
                case MotionEvent.ACTION_MOVE:
                    float dx = e.getRawX() - touch[0];
                    float dy = e.getRawY() - touch[1];
                    if (!dragging && (Math.abs(dx) > dragThreshold || Math.abs(dy) > dragThreshold)) {
                        dragging = true;
                        closePanel();
                        showDismissTarget(true);
                    }
                    if (dragging) {
                        orbLp.x = start[0] + Math.round(dx);
                        orbLp.y = clampY(start[1] + Math.round(dy), safeBounds());
                        wm.updateViewLayout(view, orbLp);
                        boolean over = isOverDismissZone(orbLp.x, orbLp.y);
                        if (over != hoveringDismiss) {
                            hoveringDismiss = over;
                            dismissView.setActive(over);
                            if (over) view.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY);
                        }
                    }
                    return true;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    orbView.setPressedGlow(false);
                    boolean wasDragging = dragging;
                    boolean shouldDismiss = hoveringDismiss && e.getActionMasked() == MotionEvent.ACTION_UP;
                    dragging = false;
                    hoveringDismiss = false;
                    showDismissTarget(false);
                    if (shouldDismiss) {
                        stopSelf();
                        return true;
                    }
                    if (!wasDragging && e.getActionMasked() == MotionEvent.ACTION_UP) {
                        togglePanel();
                    } else {
                        snapToNearestEdge();
                    }
                    scheduleIdleDim();
                    return true;
                default:
                    return false;
            }
        });

        wm.addView(orbView, orbLp);
        paint();
        scheduleIdleDim();
    }

    // ── Interactive Floating HUD Panel (decisions + live Antigravity/fleet sessions) ──────

    private void togglePanel() {
        if (panelOpen) {
            closePanel();
        } else {
            openPanel();
        }
    }

    private void openPanel() {
        if (wm == null || panelOpen) return;
        panelOpen = true;
        if (panelRoot == null) buildPanelView();
        panelLp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_DIM_BEHIND
                        | WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
                PixelFormat.TRANSLUCENT);
        panelLp.dimAmount = 0.45f;
        panelLp.gravity = Gravity.CENTER;
        panelLp.softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE;
        panelLp.layoutInDisplayCutoutMode =
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        if (Build.VERSION.SDK_INT >= 30) panelLp.setFitInsetsTypes(0);
        try {
            wm.addView(panelRoot, panelLp);
        } catch (Exception e) {
            panelOpen = false;
            return;
        }
        SafeBounds sb = safeBounds();
        updatePanelLayout(sb, sb.left, sb.top, sb.right, sb.bottom);
        panelRoot.requestApplyInsets();
        rebuildPanelContent();
        if (feed != null && !feed.isShutdown()) feed.execute(this::refresh);
    }

    private void closePanel() {
        if (!panelOpen) return;
        panelOpen = false;
        expandedSessionId = null;
        removeWindow(panelRoot);
    }

    private GradientDrawable roundedBox(int fillColor, int strokeColor, float radiusDp) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(fillColor);
        d.setCornerRadius(dp(radiusDp));
        if (strokeColor != 0) d.setStroke(dp(1), strokeColor);
        return d;
    }

    private void buildPanelView() {
        panelRoot = new FrameLayout(this) {
            @Override
            public boolean dispatchKeyEvent(KeyEvent event) {
                if (event.getKeyCode() == KeyEvent.KEYCODE_BACK && event.getAction() == KeyEvent.ACTION_UP) {
                    closePanel();
                    return true;
                }
                return super.dispatchKeyEvent(event);
            }
        };
        panelRoot.setBackgroundColor(0x55000000);
        panelRoot.setOnClickListener(v -> closePanel());

        panelSheet = new LinearLayout(this);
        panelSheet.setOrientation(LinearLayout.VERTICAL);
        panelSheet.setBackground(roundedBox(0xF50B1120, 0x5538BDF8, 20));
        panelSheet.setPadding(dp(16), dp(14), dp(16), dp(14));
        panelSheet.setClickable(true); // absorb taps inside sheet

        // Header row
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);

        LinearLayout titleBox = new LinearLayout(this);
        titleBox.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        titleBox.setLayoutParams(titleLp);

        TextView title = new TextView(this);
        title.setText("Aither · Live");
        title.setTextColor(0xFFF8FAFC);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        title.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
        titleBox.addView(title);

        panelSubtitle = new TextView(this);
        panelSubtitle.setTextColor(0xFF94A3B8);
        panelSubtitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        titleBox.addView(panelSubtitle);
        header.addView(titleBox);

        TextView refreshBtn = pillButton("Refresh", 0xFF1E293B, 0xFFCBD5E1);
        refreshBtn.setOnClickListener(v -> {
            setStatusLine("Syncing with Aither & Antigravity…", 0xFF38BDF8);
            if (feed != null && !feed.isShutdown()) feed.execute(this::refresh);
        });
        header.addView(refreshBtn);

        TextView closeBtn = pillButton("×", 0xFF1E293B, 0xFFF8FAFC);
        LinearLayout.LayoutParams closeLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        closeLp.leftMargin = dp(8);
        closeBtn.setLayoutParams(closeLp);
        closeBtn.setOnClickListener(v -> closePanel());
        header.addView(closeBtn);

        panelSheet.addView(header);

        panelStatusLine = new TextView(this);
        panelStatusLine.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        panelStatusLine.setPadding(0, dp(6), 0, dp(2));
        panelStatusLine.setVisibility(View.GONE);
        panelSheet.addView(panelStatusLine);

        // Scrollable live feed (decisions + active sessions)
        ScrollView scroll = new ScrollView(this);
        LinearLayout.LayoutParams scrollLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        scrollLp.topMargin = dp(10);
        scrollLp.bottomMargin = dp(10);
        scroll.setLayoutParams(scrollLp);

        panelContent = new LinearLayout(this);
        panelContent.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(panelContent);
        panelSheet.addView(scroll);

        // Footer actions
        LinearLayout footer = new LinearLayout(this);
        footer.setOrientation(LinearLayout.HORIZONTAL);
        footer.setGravity(Gravity.CENTER_VERTICAL);

        TextView openAppBtn = pillButton("Open Aither App", 0xFF0284C7, 0xFFFFFFFF);
        LinearLayout.LayoutParams openLp = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        openAppBtn.setGravity(Gravity.CENTER);
        openAppBtn.setPadding(dp(12), dp(10), dp(12), dp(10));
        openAppBtn.setLayoutParams(openLp);
        openAppBtn.setOnClickListener(v -> {
            closePanel();
            Intent open = new Intent(this, MainActivity.class);
            open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(open);
        });
        footer.addView(openAppBtn);

        TextView hideBtn = pillButton("Hide Orb", 0xFF1E293B, 0xFF94A3B8);
        LinearLayout.LayoutParams hideLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        hideLp.leftMargin = dp(8);
        hideBtn.setPadding(dp(12), dp(10), dp(12), dp(10));
        hideBtn.setLayoutParams(hideLp);
        hideBtn.setOnClickListener(v -> {
            closePanel();
            stopSelf();
        });
        footer.addView(hideBtn);

        panelSheet.addView(footer);

        SafeBounds sb = safeBounds();
        panelRoot.addView(panelSheet, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER));
        updatePanelLayout(sb, sb.left, sb.top, sb.right, sb.bottom);
        if (Build.VERSION.SDK_INT >= 30) {
            panelRoot.setOnApplyWindowInsetsListener((v, ins) -> {
                Insets i = ins.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                v.setPadding(i.left, i.top, i.right, i.bottom);
                updatePanelLayout(safeBounds(), i.left, i.top, i.right, i.bottom);
                return WindowInsets.CONSUMED;
            });
        }
    }

    private void updatePanelLayout(SafeBounds sb, int inLeft, int inTop, int inRight, int inBottom) {
        if (panelSheet == null) return;
        if (Build.VERSION.SDK_INT < 30 && panelRoot != null) {
            panelRoot.setPadding(inLeft, inTop, inRight, inBottom);
        }
        int availW = Math.max(dp(200), sb.width - inLeft - inRight);
        int availH = Math.max(dp(180), sb.height - inTop - inBottom);
        int cardW = Math.min(dp(370), Math.max(dp(240), availW - dp(28)));
        cardW = Math.min(cardW, Math.max(dp(200), availW - dp(16)));
        int maxH = Math.min(Math.round(availH * 0.80f), Math.max(dp(160), availH - dp(24)));
        ViewGroup.LayoutParams cur = panelSheet.getLayoutParams();
        if (cur instanceof FrameLayout.LayoutParams) {
            FrameLayout.LayoutParams lp = (FrameLayout.LayoutParams) cur;
            if (lp.width != cardW || lp.height != maxH) {
                lp.width = cardW;
                lp.height = maxH;
                lp.gravity = Gravity.CENTER;
                panelSheet.setLayoutParams(lp);
            }
        } else {
            panelSheet.setLayoutParams(new FrameLayout.LayoutParams(cardW, maxH, Gravity.CENTER));
        }
    }

    private TextView pillButton(String label, int bg, int fg) {
        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setTextColor(fg);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f);
        tv.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
        tv.setPadding(dp(12), dp(7), dp(12), dp(7));
        tv.setBackground(roundedBox(bg, 0x3394A3B8, 10));
        return tv;
    }

    private void setStatusLine(String msg, int color) {
        if (panelStatusLine == null) return;
        Runnable r = () -> {
            if (panelStatusLine == null) return;
            if (msg == null || msg.isEmpty()) {
                panelStatusLine.setVisibility(View.GONE);
            } else {
                panelStatusLine.setText(msg);
                panelStatusLine.setTextColor(color);
                panelStatusLine.setVisibility(View.VISIBLE);
            }
        };
        if (Looper.myLooper() == Looper.getMainLooper()) r.run();
        else if (ui != null) ui.post(r);
    }

    private void rebuildPanelContent() {
        if (!panelOpen || panelContent == null) return;
        FairyOverlayState s = state;
        int activeSess = s.activeSessionCount();
        if (panelSubtitle != null) {
            if (!signedInNow) {
                panelSubtitle.setText("Not signed in — tap Open Aither App to sign in");
            } else {
                panelSubtitle.setText(
                        s.cards + (s.cards == 1 ? " decision waiting" : " decisions waiting")
                                + " · " + activeSess + " active · " + s.sessions.size() + " sessions");
            }
        }
        panelContent.removeAllViews();

        if (!signedInNow) {
            panelContent.addView(emptyCard("Sign in inside Aither to sync Antigravity sessions and decision cards."));
            return;
        }

        // Section 0: Synced Favorites Rolodex (1-tap launch pills)
        List<FairyOverlayState.RolodexItem> rolodex = s.rolodex.isEmpty()
                ? FairyOverlayState.defaultRolodex()
                : s.rolodex;
        panelContent.addView(sectionHeader("★ ROLODEX · FAVORITES (" + rolodex.size() + ")"));
        panelContent.addView(buildRolodexStrip(rolodex));

        // Section 1: Decisions & Asks
        TextView decHdr = sectionHeader("NEEDS YOU · DECISIONS (" + s.cards + ")");
        LinearLayout.LayoutParams dhLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        dhLp.topMargin = dp(10);
        decHdr.setLayoutParams(dhLp);
        panelContent.addView(decHdr);
        if (s.items.isEmpty()) {
            panelContent.addView(emptyCard("No open decisions waiting right now."));
        } else {
            for (FairyOverlayState.DecisionItem item : s.items) {
                panelContent.addView(buildDecisionCard(item));
            }
        }

        // Section 2: Active Antigravity & Fleet Sessions
        TextView sessHdr = sectionHeader("LIVE SESSIONS · ANTIGRAVITY & FLEET (" + s.sessions.size() + ")");
        LinearLayout.LayoutParams shLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        shLp.topMargin = dp(12);
        sessHdr.setLayoutParams(shLp);
        panelContent.addView(sessHdr);

        if (s.sessions.isEmpty()) {
            panelContent.addView(emptyCard("No live sessions reported from your desktop daemon."));
        } else {
            for (FairyOverlayState.SessionItem sess : s.sessions) {
                panelContent.addView(buildSessionRow(sess));
            }
        }
    }

    private TextView sectionHeader(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(0xFF64748B);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10.5f);
        tv.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
        tv.setPadding(dp(2), dp(2), dp(2), dp(6));
        return tv;
    }

    private TextView emptyCard(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(0xFF94A3B8);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f);
        tv.setPadding(dp(12), dp(10), dp(12), dp(10));
        tv.setBackground(roundedBox(0xFF111827, 0x2294A3B8, 12));
        return tv;
    }

    private View buildRolodexStrip(List<FairyOverlayState.RolodexItem> items) {
        HorizontalScrollView scroll = new HorizontalScrollView(this);
        scroll.setHorizontalScrollBarEnabled(false);
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        for (FairyOverlayState.RolodexItem item : items) {
            TextView pill = new TextView(this);
            pill.setText((item.favorite ? "★ " : "") + item.title);
            pill.setTextColor(0xFFE0F2FE);
            pill.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f);
            pill.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
            pill.setPadding(dp(11), dp(7), dp(11), dp(7));
            pill.setBackground(roundedBox(0xFF0F223A, 0x6638BDF8, 18));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.rightMargin = dp(7);
            pill.setLayoutParams(lp);
            pill.setOnClickListener(v -> {
                v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP);
                launchRolodexItem(item);
            });
            row.addView(pill);
        }
        scroll.addView(row);
        return scroll;
    }

    private void launchRolodexItem(FairyOverlayState.RolodexItem item) {
        try {
            String url = item.launchUrl(AppTabs.ORIGIN);
            Intent intent = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
            closePanel();
        } catch (Exception ignored) { /* best-effort launch */ }
    }

    private View buildDecisionCard(FairyOverlayState.DecisionItem item) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackground(roundedBox(
                item.isUrgent() ? 0xFF1F1720 : 0xFF111A2E,
                item.isUrgent() ? 0x88F59E0B : 0x4438BDF8, 12));
        box.setPadding(dp(12), dp(10), dp(12), dp(10));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(8);
        box.setLayoutParams(lp);

        // Top meta line: badge + agent
        String meta = (item.isUrgent() ? "URGENT" : "DECISION")
                + (item.agent.isEmpty() ? "" : " · " + item.agent.toUpperCase(java.util.Locale.ROOT));
        TextView metaTv = new TextView(this);
        metaTv.setText(meta);
        metaTv.setTextColor(item.isUrgent() ? 0xFFFBBF24 : 0xFF38BDF8);
        metaTv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f);
        metaTv.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
        box.addView(metaTv);

        TextView titleTv = new TextView(this);
        titleTv.setText(item.title);
        titleTv.setTextColor(0xFFF8FAFC);
        titleTv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13.5f);
        titleTv.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
        titleTv.setPadding(0, dp(2), 0, dp(2));
        box.addView(titleTv);

        if (!item.summary.isEmpty()) {
            TextView sumTv = new TextView(this);
            sumTv.setText(item.summary);
            sumTv.setTextColor(0xFFCBD5E1);
            sumTv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f);
            sumTv.setMaxLines(3);
            sumTv.setEllipsize(TextUtils.TruncateAt.END);
            box.addView(sumTv);
        }

        if (!item.options.isEmpty() && !item.id.isEmpty()) {
            HorizontalScrollView hScroll = new HorizontalScrollView(this);
            hScroll.setHorizontalScrollBarEnabled(false);
            LinearLayout.LayoutParams hLp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            hLp.topMargin = dp(8);
            hScroll.setLayoutParams(hLp);

            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            for (int i = 0; i < item.options.size(); i++) {
                FairyOverlayState.OptionItem opt = item.options.get(i);
                int bg = (i == 0) ? (item.isUrgent() ? 0xFFD97706 : 0xFF0284C7) : 0xFF1E293B;
                TextView btn = pillButton(opt.label, bg, 0xFFFFFFFF);
                LinearLayout.LayoutParams bLp = new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
                if (i > 0) bLp.leftMargin = dp(6);
                btn.setLayoutParams(bLp);
                btn.setOnClickListener(v -> answerDecision(item.id, opt.key, opt.label));
                row.addView(btn);
            }
            hScroll.addView(row);
            box.addView(hScroll);
        }

        return box;
    }

    private View buildSessionRow(FairyOverlayState.SessionItem sess) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        boolean waiting = sess.isWaiting();
        boolean working = sess.isWorking();
        int border = waiting ? 0x88F59E0B : (working ? 0x5538BDF8 : 0x2294A3B8);
        box.setBackground(roundedBox(0xFF0F172A, border, 12));
        box.setPadding(dp(12), dp(9), dp(12), dp(9));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(7);
        box.setLayoutParams(lp);

        String dot = waiting ? "● WAITING" : (working ? "● WORKING" : "○ IDLE");
        int dotColor = waiting ? 0xFFFBBF24 : (working ? 0xFF38BDF8 : 0xFF94A3B8);
        String badge = dot + (sess.harness.isEmpty() ? "" : " · " + sess.harness);

        TextView head = new TextView(this);
        head.setText(badge);
        head.setTextColor(dotColor);
        head.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f);
        head.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
        box.addView(head);

        TextView title = new TextView(this);
        title.setText(sess.title);
        title.setTextColor(0xFFF1F5F9);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f);
        title.setTypeface(Typeface.SANS_SERIF, Typeface.BOLD);
        title.setMaxLines(2);
        title.setEllipsize(TextUtils.TruncateAt.END);
        box.addView(title);

        if (!sess.detail.isEmpty()) {
            TextView detail = new TextView(this);
            detail.setText(sess.detail);
            detail.setTextColor(0xFF94A3B8);
            detail.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11.5f);
            detail.setMaxLines(2);
            detail.setEllipsize(TextUtils.TruncateAt.END);
            box.addView(detail);
        }

        boolean expanded = sess.id.equals(expandedSessionId);
        box.setOnClickListener(v -> {
            expandedSessionId = expanded ? null : sess.id;
            rebuildPanelContent();
        });

        if (expanded) {
            LinearLayout steerRow = new LinearLayout(this);
            steerRow.setOrientation(LinearLayout.HORIZONTAL);
            steerRow.setGravity(Gravity.CENTER_VERTICAL);
            LinearLayout.LayoutParams sLp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            sLp.topMargin = dp(8);
            steerRow.setLayoutParams(sLp);

            EditText input = new EditText(this);
            input.setHint("Reply / steer this session…");
            input.setHintTextColor(0xFF64748B);
            input.setTextColor(0xFFF8FAFC);
            input.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f);
            input.setSingleLine(true);
            input.setPadding(dp(10), dp(7), dp(10), dp(7));
            input.setBackground(roundedBox(0xFF1E293B, 0x4438BDF8, 8));
            LinearLayout.LayoutParams iLp = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            input.setLayoutParams(iLp);
            steerRow.addView(input);

            TextView sendBtn = pillButton("Send", 0xFF0284C7, 0xFFFFFFFF);
            LinearLayout.LayoutParams bLp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            bLp.leftMargin = dp(6);
            sendBtn.setLayoutParams(bLp);
            sendBtn.setOnClickListener(v -> {
                String msg = input.getText().toString().trim();
                if (!msg.isEmpty()) {
                    input.setText("");
                    steerSession(sess.id, msg);
                }
            });
            steerRow.addView(sendBtn);
            box.addView(steerRow);
        }

        return box;
    }

    private void answerDecision(String cardId, String choiceKey, String label) {
        setStatusLine("Sending \"" + label + "\"…", 0xFF38BDF8);
        if (feed == null || feed.isShutdown()) return;
        feed.execute(() -> {
            try {
                String cookies = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
                if (cookies == null || !cookies.contains(Session.COOKIE + "=")) {
                    setStatusLine("Sign in to Aither first.", 0xFFF87171);
                    return;
                }
                String path = "/api/decisions/" + URLEncoder.encode(cardId, "UTF-8") + "/answer";
                String body = "{\"choice\":" + FairyOverlayState.jsonQuote(choiceKey)
                        + ",\"note\":\"\",\"via\":\"phone-orb\"}";
                int code = postJson(AppTabs.ORIGIN + path, cookies, body);
                if (code >= 200 && code < 300) {
                    setStatusLine("Answered: " + label, 0xFF4ADE80);
                    refresh();
                } else {
                    setStatusLine("Could not answer (HTTP " + code + ")", 0xFFF87171);
                }
            } catch (Exception e) {
                setStatusLine("Send failed: offline?", 0xFFF87171);
            }
        });
    }

    private void steerSession(String sessionId, String text) {
        setStatusLine("Steering session…", 0xFF38BDF8);
        if (feed == null || feed.isShutdown()) return;
        feed.execute(() -> {
            try {
                String cookies = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
                if (cookies == null || !cookies.contains(Session.COOKIE + "=")) {
                    setStatusLine("Sign in to Aither first.", 0xFFF87171);
                    return;
                }
                String path = "/api/decisions/sessions/" + URLEncoder.encode(sessionId, "UTF-8") + "/message";
                String body = "{\"text\":" + FairyOverlayState.jsonQuote(text) + "}";
                int code = postJson(AppTabs.ORIGIN + path, cookies, body);
                if (code >= 200 && code < 300) {
                    expandedSessionId = null;
                    setStatusLine("Sent to session ✓", 0xFF4ADE80);
                    refresh();
                } else {
                    setStatusLine("Could not send (HTTP " + code + ")", 0xFFF87171);
                }
            } catch (Exception e) {
                setStatusLine("Send failed: offline?", 0xFFF87171);
            }
        });
    }

    private int postJson(String url, String cookies, String jsonBody) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(8000);
        c.setReadTimeout(20000);
        c.setDoOutput(true);
        c.setRequestProperty("Accept", "application/json");
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Origin", AppTabs.ORIGIN);
        c.setRequestProperty("Cookie", cookies);
        byte[] bytes = jsonBody.getBytes(StandardCharsets.UTF_8);
        try (OutputStream out = c.getOutputStream()) {
            out.write(bytes);
        }
        try {
            return c.getResponseCode();
        } finally {
            c.disconnect();
        }
    }

    private String getJson(String url, String cookies) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(15000);
        c.setRequestProperty("Accept", "application/json");
        c.setRequestProperty("Cookie", cookies);
        try {
            if (c.getResponseCode() != 200) return null;
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            try (InputStream in = c.getInputStream()) {
                byte[] b = new byte[8192];
                int n;
                while ((n = in.read(b)) > 0) buf.write(b, 0, n);
            }
            return buf.toString(StandardCharsets.UTF_8.name());
        } finally {
            c.disconnect();
        }
    }

    private void showDismissTarget(boolean show) {
        if (dismissView == null) return;
        dismissView.setActive(false);
        dismissView.setVisibility(show ? View.VISIBLE : View.GONE);
    }

    private boolean isOverDismissZone(int orbX, int orbY) {
        SafeBounds sb = safeBounds();
        float orbCx = orbX + orbSizePx * 0.5f;
        float orbCy = orbY + orbSizePx * 0.5f;
        float targetCx = sb.left + (sb.width - sb.left - sb.right) * 0.5f;
        float targetCy = sb.height - sb.bottom - dp(16) - dismissSizePx * 0.5f;
        float dx = orbCx - targetCx;
        float dy = orbCy - targetCy;
        float radius = dp(64);
        return (dx * dx + dy * dy) <= (radius * radius);
    }

    private void cancelSnap() {
        if (snapAnim != null) {
            snapAnim.cancel();
            snapAnim = null;
        }
    }

    private void snapToNearestEdge() {
        if (orbView == null || orbLp == null || wm == null) return;
        SafeBounds sb = safeBounds();
        int center = orbLp.x + orbSizePx / 2;
        int midX = sb.left + (sb.width - sb.left - sb.right) / 2;
        boolean dockRight = center >= midX;
        int targetX = edgeX(dockRight, sb);
        int clampedY = clampY(orbLp.y, sb);
        orbLp.y = clampedY;

        int minY = sb.top + dp(8);
        int maxY = Math.max(minY, sb.height - sb.bottom - orbSizePx - dp(8));
        float yFrac = maxY > minY ? (float) (clampedY - minY) / (float) (maxY - minY) : 0.36f;
        getSharedPreferences(PREFS, MODE_PRIVATE)
                .edit()
                .putBoolean(KEY_DOCK_RIGHT, dockRight)
                .putFloat(KEY_Y_FRACTION, Math.max(0f, Math.min(1f, yFrac)))
                .apply();

        final int fromX = orbLp.x;
        cancelSnap();
        snapAnim = ValueAnimator.ofInt(fromX, targetX);
        snapAnim.setDuration(220);
        snapAnim.setInterpolator(new DecelerateInterpolator(2.2f));
        snapAnim.addUpdateListener(anim -> {
            if (orbView == null || orbLp == null || wm == null) return;
            orbLp.x = (Integer) anim.getAnimatedValue();
            try {
                wm.updateViewLayout(orbView, orbLp);
            } catch (Exception ignored) { /* detached */ }
        });
        snapAnim.start();
    }

    private void wakeAlpha() {
        if (ui != null) ui.removeCallbacks(idleDimTask);
        if (orbView != null) orbView.animate().alpha(1f).setDuration(140).start();
    }

    private void scheduleIdleDim() {
        if (ui == null) return;
        ui.removeCallbacks(idleDimTask);
        ui.postDelayed(idleDimTask, IDLE_DIM_MS);
    }

    private void applyIdleAlpha() {
        if (orbView == null || dragging || panelOpen) return;
        FairyOverlayState s = state;
        float target = (s.speaking || s.attention || s.anySessionWaiting()) ? 1f : IDLE_ALPHA;
        orbView.animate().alpha(target).setDuration(260).start();
    }

    private void syncAppVisibility() {
        if (orbView == null) return;
        if (appInForeground) closePanel();
        int want = appInForeground ? View.GONE : View.VISIBLE;
        if (orbView.getVisibility() != want) {
            orbView.setVisibility(want);
            if (want == View.VISIBLE) {
                orbView.invalidate();
                scheduleNextAnimFrame();
                applyIdleAlpha();
            } else {
                showDismissTarget(false);
                if (ui != null) ui.removeCallbacks(animFrameTask);
                animScheduled = false;
            }
        }
    }

    private void scheduleNextAnimFrame() {
        if (ui == null || animScheduled || orbView == null || orbView.getVisibility() != View.VISIBLE) {
            return;
        }
        // Gentle 30fps ambient motion while visible outside Aither; 0% CPU when hidden inside app.
        animScheduled = true;
        ui.postDelayed(animFrameTask, 33L);
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        density = getResources().getDisplayMetrics().density;
        if (wm == null) return;
        SafeBounds sb = safeBounds();
        if (orbView != null && orbLp != null) {
            SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
            boolean dockRight = prefs.getBoolean(KEY_DOCK_RIGHT, true);
            float yFrac = prefs.getFloat(KEY_Y_FRACTION, 0.36f);
            orbLp.x = edgeX(dockRight, sb);
            int minY = sb.top + dp(8);
            int maxY = Math.max(minY, sb.height - sb.bottom - orbSizePx - dp(8));
            orbLp.y = clampY(minY + Math.round((maxY - minY) * yFrac), sb);
            try {
                wm.updateViewLayout(orbView, orbLp);
            } catch (Exception ignored) { /* detached */ }
        }
        if (dismissView != null && dismissLp != null) {
            dismissLp.x = (sb.left - sb.right) / 2;
            dismissLp.y = sb.bottom + dp(16);
            try {
                wm.updateViewLayout(dismissView, dismissLp);
            } catch (Exception ignored) { /* detached */ }
        }
        if (panelOpen && panelRoot != null) {
            updatePanelLayout(sb, sb.left, sb.top, sb.right, sb.bottom);
            panelRoot.requestApplyInsets();
        }
    }

    // ── the state feed: decisions + live Antigravity / fleet sessions ───────────────────

    private void startFeed() {
        feed = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "fairy-feed");
            t.setDaemon(true);
            return t;
        });
        feed.scheduleWithFixedDelay(this::refresh, 0, 30, TimeUnit.SECONDS);
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (cm != null) {
                net = new ConnectivityManager.NetworkCallback() {
                    @Override public void onAvailable(Network n) { feed.execute(FairyOverlayService.this::refresh); }
                    @Override public void onCapabilitiesChanged(Network n, NetworkCapabilities c) {
                        if (c.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                            feed.execute(FairyOverlayService.this::refresh);
                        }
                    }
                };
                cm.registerNetworkCallback(
                        new NetworkRequest.Builder()
                                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build(), net);
            }
        } catch (Exception e) {
            android.util.Log.w("FairyOverlay", "no network watch: " + e);
        }
        ui.postDelayed(this::speakTick, 400);
        scheduleNextAnimFrame();
    }

    private void speakTick() {
        boolean on = AitherSay.speaking;
        if (on != state.speaking) {
            state = state.withSpeaking(on);
            render();
        }
        if (feed != null && !feed.isShutdown()) ui.postDelayed(this::speakTick, 400);
    }

    /** Read /api/decisions?status=open and /api/decisions/sessions with this app's WebView session. */
    private void refresh() {
        try {
            String cookies = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
            boolean signedIn = cookies != null && cookies.contains(Session.COOKIE + "=");
            signedInNow = signedIn;
            if (!signedIn) {
                state = FairyOverlayState.empty().withSpeaking(AitherSay.speaking);
                render();
                return;
            }
            String decJson = getJson(AppTabs.ORIGIN + "/api/decisions?status=open", cookies);
            FairyOverlayState next = decJson != null
                    ? FairyOverlayState.parse(decJson, AitherSay.speaking)
                    : state.withSpeaking(AitherSay.speaking);

            List<FairyOverlayState.SessionItem> sessList = state.sessions;
            try {
                String sessJson = getJson(AppTabs.ORIGIN + "/api/decisions/sessions", cookies);
                if (sessJson != null) sessList = FairyOverlayState.parseSessions(sessJson);
            } catch (Exception ignored) { /* keep last sessions on partial failure */ }

            List<FairyOverlayState.RolodexItem> rolodexList = state.rolodex;
            try {
                String prefsJson = getJson(AppTabs.ORIGIN + "/api/settings/preferences", cookies);
                if (prefsJson != null) rolodexList = FairyOverlayState.parseRolodex(prefsJson);
            } catch (Exception ignored) { /* keep last rolodex on partial failure */ }

            state = next.withSessions(sessList).withRolodex(rolodexList);
            render();
        } catch (Exception e) {
            // offline: keep the last reading until the next poll or reconnect
        }
    }

    private void render() {
        if (ui == null || Looper.myLooper() == Looper.getMainLooper()) paint();
        else ui.post(this::paint);
    }

    private void paint() {
        if (orbView == null) return;
        FairyOverlayState s = state;
        orbView.setContentDescription(s.description());
        orbView.setFairyState(s);
        if (s.attention || s.speaking || s.anySessionWaiting()) {
            wakeAlpha();
        } else if (!dragging && !panelOpen) {
            scheduleIdleDim();
        }
        if (panelOpen) {
            rebuildPanelContent();
        }
    }

    @Override
    public void onDestroy() {
        if (activeInstance == this) activeInstance = null;
        cancelSnap();
        closePanel();
        if (ui != null) {
            ui.removeCallbacks(idleDimTask);
            ui.removeCallbacks(animFrameTask);
        }
        animScheduled = false;
        if (feed != null) feed.shutdownNow();
        if (net != null) {
            try {
                ((ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE)).unregisterNetworkCallback(net);
            } catch (Exception ignored) { /* already gone */ }
        }
        removeWindow(orbView);
        removeWindow(dismissView);
        orbView = null;
        dismissView = null;
        panelRoot = null;
        super.onDestroy();
    }

    private void removeWindow(View v) {
        if (wm != null && v != null) {
            try {
                wm.removeView(v);
            } catch (Exception ignored) { /* already gone */ }
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ── Custom Canvas Views (zero emoji, zero external assets) ───────────────────────────

    private final class FairyOrbView extends View {
        private final Paint haloPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint corePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint rimPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint motePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint badgeBgPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint badgeRingPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint badgeTextPaint = new Paint(Paint.ANTI_ALIAS_FLAG);

        private FairyOverlayState current = FairyOverlayState.empty();
        private boolean pressedGlow = false;

        FairyOrbView(Context ctx) {
            super(ctx);
            rimPaint.setStyle(Paint.Style.STROKE);
            rimPaint.setStrokeWidth(1.2f * density);

            badgeRingPaint.setStyle(Paint.Style.STROKE);
            badgeRingPaint.setStrokeWidth(1.4f * density);
            badgeRingPaint.setColor(0xFF0A0E14);

            badgeTextPaint.setTextAlign(Paint.Align.CENTER);
            badgeTextPaint.setTypeface(Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD));
            badgeTextPaint.setTextSize(10.5f * density);
        }

        void setFairyState(FairyOverlayState s) {
            this.current = s == null ? FairyOverlayState.empty() : s;
            invalidate();
        }

        void setPressedGlow(boolean on) {
            this.pressedGlow = on;
            invalidate();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float w = getWidth();
            float h = getHeight();
            if (w <= 0 || h <= 0) return;

            float cx = w * 0.5f;
            float cy = h * 0.5f;
            float t = SystemClock.uptimeMillis() * 0.001f;
            boolean urgent = current.attention || current.anySessionWaiting();
            float breath = (float) Math.sin(t * (current.speaking ? 7.5f : 2.1f));
            float pulse = current.speaking ? (0.12f * breath) : (0.04f * breath);

            // Color palette: calm ice-cyan | warm amber when Needs You | luminous white-cyan when speaking.
            int haloInner = urgent ? 0xCCFFB84D : (current.speaking ? 0xDD99F6FF : 0xAA38BDF8);
            int haloMid   = urgent ? 0x55F59E0B : 0x440EA5E9;
            int coreLight = urgent ? 0xFFFFFBEB : 0xFFF0FDFF;
            int coreMid   = urgent ? 0xFFFBBF24 : 0xFF67E8F9;
            int coreEdge  = urgent ? 0xFFB45309 : 0xFF0369A1;

            float haloR = (w * 0.45f) * (1f + pulse + (pressedGlow ? 0.06f : 0f));
            haloPaint.setShader(new RadialGradient(
                    cx, cy, Math.max(1f, haloR),
                    new int[]{haloInner, haloMid, 0x00000000},
                    new float[]{0.18f, 0.58f, 1f},
                    Shader.TileMode.CLAMP));
            canvas.drawCircle(cx, cy, haloR, haloPaint);

            // Glassmorphic core orb
            float coreR = (w * 0.255f) * (1f + pulse * 0.5f);
            corePaint.setShader(new RadialGradient(
                    cx - coreR * 0.28f, cy - coreR * 0.28f, Math.max(1f, coreR * 1.35f),
                    new int[]{coreLight, coreMid, coreEdge},
                    new float[]{0.0f, 0.55f, 1.0f},
                    Shader.TileMode.CLAMP));
            canvas.drawCircle(cx, cy, coreR, corePaint);

            // Crisp luminous rim
            rimPaint.setColor(urgent ? 0xBBFDE68A : 0xBBE0F2FE);
            canvas.drawCircle(cx, cy, coreR, rimPaint);

            // 3 subtle orbiting spirit motes
            float orbitR = w * 0.34f;
            int[] moteColors = urgent
                    ? new int[]{0xFFFDE68A, 0xFFFBBF24, 0xFFFED7AA}
                    : new int[]{0xFFBAE6FD, 0xFF67E8F9, 0xFFC4B5FD};
            for (int i = 0; i < 3; i++) {
                double ang = t * (current.speaking ? 2.4 : 0.85) + (i * Math.PI * 2.0 / 3.0);
                float mx = cx + (float) Math.cos(ang) * orbitR;
                float my = cy + (float) Math.sin(ang) * orbitR * 0.72f;
                float mr = (i == 0 ? 2.4f : 1.9f) * density;
                motePaint.setColor(moteColors[i]);
                canvas.drawCircle(mx, my, mr, motePaint);
            }

            // Crisp top-right count pill when cards wait (no emoji)
            String badge = current.badgeText();
            if (!badge.isEmpty()) {
                float br = 9.2f * density;
                float bx = w - br - 2.5f * density;
                float by = br + 2.5f * density;
                badgeBgPaint.setColor(urgent ? 0xFFF59E0B : 0xFF0EA5E9);
                canvas.drawCircle(bx, by, br, badgeBgPaint);
                canvas.drawCircle(bx, by, br, badgeRingPaint);
                badgeTextPaint.setColor(urgent ? 0xFF0A0E14 : Color.WHITE);
                Paint.FontMetrics fm = badgeTextPaint.getFontMetrics();
                float ty = by - (fm.ascent + fm.descent) * 0.5f;
                canvas.drawText(badge, bx, ty, badgeTextPaint);
            }
        }
    }

    private final class DismissTargetView extends View {
        private final Paint bgPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint ringPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint xPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private boolean active = false;

        DismissTargetView(Context ctx) {
            super(ctx);
            ringPaint.setStyle(Paint.Style.STROKE);
            ringPaint.setStrokeWidth(2f * density);
            xPaint.setStyle(Paint.Style.STROKE);
            xPaint.setStrokeCap(Paint.Cap.ROUND);
            xPaint.setStrokeWidth(2.4f * density);
        }

        void setActive(boolean on) {
            if (this.active == on) return;
            this.active = on;
            invalidate();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float cx = getWidth() * 0.5f;
            float cy = getHeight() * 0.5f;
            float r = Math.min(cx, cy) - 3f * density;
            bgPaint.setColor(active ? 0xDD991B1B : 0xCC0F172A);
            ringPaint.setColor(active ? 0xFFF87171 : 0x8894A3B8);
            xPaint.setColor(active ? Color.WHITE : 0xFFE2E8F0);
            canvas.drawCircle(cx, cy, r, bgPaint);
            canvas.drawCircle(cx, cy, r, ringPaint);
            float arm = r * 0.34f;
            canvas.drawLine(cx - arm, cy - arm, cx + arm, cy + arm, xPaint);
            canvas.drawLine(cx + arm, cy - arm, cx - arm, cy + arm, xPaint);
        }
    }
}
