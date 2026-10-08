package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanRecord;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;
import android.os.SystemClock;
import android.view.View;
import android.webkit.CookieManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Collections;
import java.util.List;

/**
 * Settings > Nearby devices: the member's phone finds a watch or a new phone that is in
 * "Add to my devices" (BleCandidate), checks the six-digit SAS with its owner, and, on
 * Approve, hands it a single-use workspace pairing code sealed for that handshake
 * (BlePair.Approver). The device then confirms the code with Identity itself.
 *
 * <p>Scanning runs only while this screen is in front (stopped in onPause) and for
 * BleNearby.SCAN_MS per "Look again"; never in the background, never for location
 * (BLUETOOTH_SCAN is declared neverForLocation, and Android 11 and older -- which would need
 * the location permission to scan -- are sent to the code flow instead).
 *
 * <p>Who may approve is decided by the server: the code is minted by Identity's
 * POST /v1/nodes/pairing/init (through the portal's /api/me/machines with this app's
 * session), which refuses a child account; the refusal here is only the words.
 *
 * <p><b>Seams.</b> {@link #codes} is where the code comes from; {@link #fallback} is where
 * the internet join-request flow (Phase 1, feat/device-join-same-account-qr) plugs in, offered
 * whenever Bluetooth cannot finish. With no fallback set, "Use a code instead" opens
 * LinkActivity, the device-code approval that already exists.
 */
public class NearbyDevicesActivity extends Activity {
    /** Mints the single-use pairing code for a device the person approved. */
    interface CodeSource {
        /** The code, or null with the words in {@code why[0]}. Blocking; off the main thread. */
        String mint(int deviceClass, String[] why);
    }

    /** An internet path for when Bluetooth can't finish (Phase 1's join request). */
    interface Fallback {
        /** True when it took over the screen. */
        boolean offer(Activity from, int deviceClass);
    }

    static volatile CodeSource codes = NearbyDevicesActivity::portalCode;
    static volatile Fallback fallback = null;

    private static final int ASK_BLE = 11;
    private static final long HANDSHAKE_MS = BlePair.HANDSHAKE_MS;

    private enum Step { NONE, HELLO, REVEAL, ASK, CODE, DONE }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final BleNearby nearby = new BleNearby();
    private final SecureRandom rnd = new SecureRandom();
    private Config cfg;
    private LinearLayout body;
    private LinearLayout list;
    private TextView status;
    private BluetoothLeScanner scanner;
    private boolean scanning;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic rx;
    private BluetoothGattCharacteristic tx;
    private BlePair.Approver approver;
    private int pickedClass;
    private volatile Step step = Step.NONE;
    private boolean dropped;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        cfg = new Config(this);
        body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(this, 16);
        body.setPadding(pad, pad, pad, pad * 2);
        android.widget.ScrollView scroll = new android.widget.ScrollView(this);
        scroll.addView(body);
        LinearLayout screen = new LinearLayout(this);
        screen.setOrientation(LinearLayout.VERTICAL);
        screen.setBackgroundColor(Ui.BG);
        LinearLayout bar = Ui.bar(this);
        bar.addView(Ui.icon(this, R.drawable.ic_nav_back, "Back", v -> finish()));
        TextView title = Ui.barTitle(this);
        title.setText("Nearby devices");
        bar.addView(title);
        screen.addView(bar);
        screen.addView(Ui.rule(this));
        screen.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(screen);
        Edge.fit(screen);
        home();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (dropped) {
            dropped = false;
            failed("Stopped when you left the screen. Nothing was added.", pickedClass);
        }
    }

    @Override
    protected void onPause() {
        // never in the background: leaving the screen ends the scan and any handshake
        stopScan();
        if (step != Step.DONE && step != Step.NONE) {
            drop();
            dropped = true;
        }
        super.onPause();
    }

    // ------------------------------------------------------------------ screens

    private void home() {
        body.removeAllViews();
        step = Step.NONE;
        String no = DeviceLink.childRefusal(cfg.profileKind(), cfg.childDevice());
        if (no != null) {
            body.addView(Ui.note(this, no));
            body.addView(Ui.action(this, "Close", v -> finish()));
            return;
        }
        if (Build.VERSION.SDK_INT < 31) {
            body.addView(Ui.note(this, "Finding devices over Bluetooth needs Android 12 or newer. "
                    + "Use the code your device shows instead."));
            body.addView(Ui.action(this, "Use a code instead", v -> useCode(0)));
            return;
        }
        body.addView(Ui.note(this, "On the watch or new phone, open Aither and tap Add to my devices. "
                + "It shows up here while both screens are open."));
        status = Ui.text(this, "", 13, Ui.DIM);
        body.addView(status);
        list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        body.addView(list);
        body.addView(Ui.action(this, "Look again", v -> scan()));
        body.addView(Ui.action(this, "Use a code instead", v -> useCode(0)));
        scan();
    }

    private void useCode(int deviceClass) {
        Fallback f = fallback;
        if (f != null && f.offer(this, deviceClass)) return;
        startActivity(new Intent(this, LinkActivity.class));
    }

    private void failed(String why, int deviceClass) {
        drop();
        body.removeAllViews();
        body.addView(Ui.note(this, why));
        body.addView(Ui.action(this, "Look again", v -> home()));
        body.addView(Ui.action(this, "Use a code instead", v -> useCode(deviceClass)));
    }

    // ------------------------------------------------------------------ scanning

    private boolean granted() {
        return checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
                && checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
    }

    private void scan() {
        if (!granted()) {
            requestPermissions(new String[] {Manifest.permission.BLUETOOTH_SCAN,
                    Manifest.permission.BLUETOOTH_CONNECT}, ASK_BLE);
            return;
        }
        BluetoothManager bm = getSystemService(BluetoothManager.class);
        BluetoothAdapter a = bm == null ? null : bm.getAdapter();
        if (a == null || !a.isEnabled()) {
            status.setText("Turn Bluetooth on, then tap Look again.");
            return;
        }
        stopScan();
        scanner = a.getBluetoothLeScanner();
        if (scanner == null) {
            status.setText("Bluetooth isn't ready. Tap Look again.");
            return;
        }
        nearby.clear();
        // only adverts carrying the Aither pairing service data, version 1
        ScanFilter only = new ScanFilter.Builder()
                .setServiceData(new ParcelUuid(BleCandidate.SERVICE), new byte[] {(byte) BlePair.VERSION},
                        new byte[] {(byte) 0xFF})
                .build();
        ScanSettings s = new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        try {
            scanner.startScan(Collections.singletonList(only), s, scanCb);
            scanning = true;
        } catch (SecurityException e) {
            status.setText("Aither needs the Nearby devices permission for this.");
            return;
        }
        status.setText("Looking…");
        render();
        main.postDelayed(scanBox, BleNearby.SCAN_MS);
    }

    private final Runnable scanBox = () -> {
        stopScan();
        if (status != null && step == Step.NONE) status.setText("Stopped looking. Tap Look again.");
    };

    private void stopScan() {
        main.removeCallbacks(scanBox);
        if (scanning && scanner != null) {
            try { scanner.stopScan(scanCb); } catch (SecurityException | IllegalStateException e) { /* off */ }
        }
        scanning = false;
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code != ASK_BLE) return;
        if (granted()) {
            scan();
        } else if (status != null) {
            status.setText("Without Nearby devices Aither can't find it. Use a code instead.");
        }
    }

    private final ScanCallback scanCb = new ScanCallback() {
        @Override
        public void onScanResult(int type, ScanResult r) {
            ScanRecord rec = r.getScanRecord();
            byte[] data = rec == null ? null : rec.getServiceData(new ParcelUuid(BleCandidate.SERVICE));
            if (nearby.seen(data, r.getRssi(), r.getDevice(), SystemClock.elapsedRealtime())) {
                main.post(NearbyDevicesActivity.this::render);
            }
        }

        @Override
        public void onScanFailed(int errorCode) {
            main.post(() -> { if (status != null) status.setText("Bluetooth couldn't look (" + errorCode + ")."); });
        }
    };

    private void render() {
        if (list == null || step != Step.NONE) return;
        long now = SystemClock.elapsedRealtime();
        list.removeAllViews();
        List<BleNearby.Device> ds = nearby.list(now);
        if (nearby.flooded(now)) {
            list.addView(Ui.note(this, "A lot of devices are calling out at once. Hold your phone "
                    + "next to the one you are adding."));
        }
        if (ds.isEmpty() && scanning) list.addView(Ui.note(this, "Nothing yet."));
        for (BleNearby.Device d : ds) {
            final String rid = d.rid;
            list.addView(Ui.action(this, d.label(), v -> pick(rid)));
        }
        if (scanning) main.postDelayed(this::render, 2000);
    }

    // ------------------------------------------------------------------ one handshake

    private void pick(String rid) {
        BleNearby.Device d = nearby.get(rid, SystemClock.elapsedRealtime());
        if (d == null || !(d.handle instanceof BluetoothDevice)) {
            status.setText("That one went away. Tap Look again.");
            return;
        }
        stopScan();
        step = Step.HELLO;
        pickedClass = d.advert.deviceClass;
        approver = new BlePair.Approver(d.advert, rnd);
        body.removeAllViews();
        body.addView(Ui.note(this, "Connecting to " + BlePair.label(pickedClass).toLowerCase() + "…"));
        body.addView(Ui.action(this, "Cancel", v -> { drop(); home(); }));
        main.postDelayed(handshakeBox, HANDSHAKE_MS);
        try {
            gatt = ((BluetoothDevice) d.handle).connectGatt(this, false, gattCb, BluetoothDevice.TRANSPORT_LE);
        } catch (SecurityException e) {
            failed("Aither needs the Nearby devices permission for this.", pickedClass);
        }
    }

    private final Runnable handshakeBox = () -> {
        if (step != Step.DONE && step != Step.NONE) failed("That took too long. Nothing was added.", pickedClass);
    };

    /** Close the connection and forget the handshake (the device frees itself too). */
    private void drop() {
        main.removeCallbacks(handshakeBox);
        BluetoothGatt g = gatt;
        gatt = null;
        rx = null;
        tx = null;
        approver = null;
        if (g != null) {
            try {
                g.disconnect();
                g.close();
            } catch (SecurityException e) { /* already gone */ }
        }
        if (step != Step.DONE) step = Step.NONE;
    }

    private void fromRadio(String why) {
        main.post(() -> { if (step != Step.DONE && step != Step.NONE) failed(why, pickedClass); });
    }

    @SuppressWarnings("deprecation")
    private boolean write(byte[] v) {
        BluetoothGatt g = gatt;
        if (g == null || rx == null) return false;
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                return g.writeCharacteristic(rx, v, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)
                        == BluetoothGatt.GATT_SUCCESS;
            }
            rx.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
            rx.setValue(v);
            return g.writeCharacteristic(rx);
        } catch (SecurityException e) {
            return false;
        }
    }

    private final BluetoothGattCallback gattCb = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int st, int newState) {
            try {
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    if (!g.requestMtu(247)) g.discoverServices();
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    fromRadio("Lost the connection. Bring the phone closer and look again.");
                }
            } catch (SecurityException e) {
                fromRadio("Aither needs the Nearby devices permission for this.");
            }
        }

        @Override
        public void onMtuChanged(BluetoothGatt g, int mtu, int st) {
            try {
                g.discoverServices();
            } catch (SecurityException e) {
                fromRadio("Aither needs the Nearby devices permission for this.");
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int st) {
            BluetoothGattService svc = g.getService(BleCandidate.SERVICE);
            if (st != BluetoothGatt.GATT_SUCCESS || svc == null) {
                fromRadio("That device isn't ready to be added.");
                return;
            }
            rx = svc.getCharacteristic(BleCandidate.RX);
            tx = svc.getCharacteristic(BleCandidate.TX);
            BlePair.Approver a = approver;
            if (rx == null || tx == null || a == null || !write(a.hello())) {
                fromRadio("That device isn't ready to be added.");
            }
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic ch, int st) {
            if (st != BluetoothGatt.GATT_SUCCESS) {
                fromRadio("That device didn't take it.");
                return;
            }
            try {
                if (!g.readCharacteristic(tx)) fromRadio("That device didn't answer.");
            } catch (SecurityException e) {
                fromRadio("Aither needs the Nearby devices permission for this.");
            }
        }

        @Override
        public void onCharacteristicRead(BluetoothGatt g, BluetoothGattCharacteristic ch, byte[] value, int st) {
            answered(st, value);
        }

        @Override
        @SuppressWarnings("deprecation")
        public void onCharacteristicRead(BluetoothGatt g, BluetoothGattCharacteristic ch, int st) {
            if (Build.VERSION.SDK_INT < 33) answered(st, ch.getValue());
        }
    };

    /** The device's answer to our last write, in step order. */
    private void answered(int st, byte[] value) {
        BlePair.Approver a = approver;
        if (st != BluetoothGatt.GATT_SUCCESS || a == null) {
            fromRadio("That device didn't answer.");
            return;
        }
        switch (step) {
            case HELLO: {
                byte[] reveal = a.onKey(value);
                if (reveal == null) { fromRadio(a.why); return; }
                step = Step.REVEAL;
                if (!write(reveal)) fromRadio("That device didn't take it.");
                return;
            }
            case REVEAL:
                if (!a.onSasShown(value)) { fromRadio(a.why); return; }
                step = Step.ASK;
                String sas = a.sas();
                main.post(() -> ask(sas));
                return;
            case CODE:
                if (BlePair.Approver.delivered(value)) {
                    step = Step.DONE;
                    main.post(this::sent);
                } else {
                    fromRadio(value != null && value.length == 1
                            ? BlePair.Approver.refusal(value[0]) : "That device refused.");
                }
                return;
            default:
        }
    }

    private void ask(String sas) {
        if (step != Step.ASK) return;
        body.removeAllViews();
        TextView q = Ui.text(this, "Does " + BlePair.label(pickedClass).toLowerCase() + " show", 16, Ui.INK);
        body.addView(q);
        TextView big = Ui.text(this, sas, 34, Ui.INK);
        big.setTypeface(android.graphics.Typeface.MONOSPACE);
        big.setPadding(0, Ui.dp(this, 8), 0, Ui.dp(this, 8));
        body.addView(big);
        body.addView(Ui.note(this, "Only approve if the numbers match. It joins your devices, "
                + "and you can remove it later in Settings."));
        TextView result = Ui.text(this, "", 14, Ui.DIM);
        TextView approve = Ui.action(this, "Approve", null);
        approve.setOnClickListener(v -> {
            approve.setEnabled(false);
            result.setText("Approving…");
            final int cls = pickedClass;
            // the code is for THIS handshake only: a late mint never reaches another device
            final BlePair.Approver mine = approver;
            new Thread(() -> {
                String[] why = {null};
                String code = codes.mint(cls, why);
                main.post(() -> deliver(mine, code, why[0]));
            }, "aither-nearby-mint").start();
        });
        body.addView(approve);
        body.addView(Ui.action(this, "They don't match", v -> {
            failed("Stopped. Nothing was added. If someone else's device answered, "
                    + "move closer to yours and look again.", pickedClass);
        }));
        body.addView(result);
    }

    private void deliver(BlePair.Approver mine, String code, String why) {
        // approved for one handshake; that one ended (or another device was picked since)
        if (step != Step.ASK || mine == null || approver != mine) return;
        BlePair.Approver a = mine;
        byte[] msg = code == null || a == null ? null : a.codeMessage(code);
        if (msg == null) {
            failed(why == null ? "Aither couldn't approve it." : why, pickedClass);
            return;
        }
        step = Step.CODE;
        if (!write(msg)) failed("That device didn't take it.", pickedClass);
    }

    private void sent() {
        main.removeCallbacks(handshakeBox);
        body.removeAllViews();
        body.addView(Ui.note(this, "Approved. Tap Matches on " + BlePair.label(pickedClass).toLowerCase()
                + " to finish; it joins your devices in a moment."));
        body.addView(Ui.action(this, "Done", v -> finish()));
        BluetoothGatt g = gatt;
        gatt = null;
        // give the device a moment to read nothing more, then let go
        main.postDelayed(() -> {
            if (g != null) {
                try { g.disconnect(); g.close(); } catch (SecurityException e) { /* gone */ }
            }
        }, 1500);
    }

    // ------------------------------------------------------------------ the code

    /**
     * The default CodeSource: the portal's POST /api/me/machines with this app's session,
     * which is Identity's pairing/init for the signed-in person (plan, device cap and the
     * child refusal are all decided there).
     */
    static String portalCode(int deviceClass, String[] why) {
        String cookie = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
        if (!Session.hasSession(cookie)) {
            why[0] = "Sign in to Aither on this phone first.";
            return null;
        }
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(AppTabs.ORIGIN + "/api/me/machines").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(10000);
            c.setReadTimeout(20000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Cookie", cookie);
            try (OutputStream o = c.getOutputStream()) { o.write("{}".getBytes(StandardCharsets.UTF_8)); }
            int st = c.getResponseCode();
            JSONObject j;
            try (InputStream in = st < 400 ? c.getInputStream() : c.getErrorStream()) {
                j = new JSONObject(read(in));
            } catch (Exception e) {
                j = new JSONObject();
            }
            String code = j.optString("code", "");
            if (st == 200 && BlePair.validCode(code)) return code;
            why[0] = mintRefusal(st, j.optString("error", ""));
            return null;
        } catch (Exception e) {
            why[0] = "No connection. Check the internet and try again.";
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String read(InputStream in) throws java.io.IOException {
        if (in == null) return "{}";
        java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[2048];
        int n;
        while (b.size() < 65536 && (n = in.read(buf)) > 0) b.write(buf, 0, n);
        return b.toString("UTF-8");
    }

    static String mintRefusal(int status, String error) {
        if ("child_account".equals(error)) {
            return "A grown-up in your family adds new devices. Ask them to do it from their phone.";
        }
        if (status == 401) return "Sign in to Aither on this phone first.";
        if (status == 403) return "This account can't add devices.";
        if (status == 402) {
            return "no_room".equals(error) ? "Your plan has no room for another device."
                    : "Adding devices needs a plan.";
        }
        if (status == 429) return "Too many devices are being added right now. Wait a few minutes.";
        return "Aither couldn't approve it (" + status + ").";
    }
}
