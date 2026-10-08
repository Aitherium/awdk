package com.aitherium.aither;

import android.Manifest;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattServer;
import android.bluetooth.BluetoothGattServerCallback;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;

/**
 * The radio half of "add this device nearby", for the device being added (the watch, or a
 * new phone): a BLE advert (BlePair.Advert, rotated) and a GATT server with the pairing
 * service, both alive only between {@link #start} and {@link #stop} -- the screen that asks
 * for it calls stop in onPause, and BlePair.LIFETIME_MS closes it regardless. Every
 * decision (who may write, what is answered, when the code is released) is BlePair.Candidate;
 * this class only moves bytes. Nothing here logs a message, a key or a code.
 *
 * Shared by the phone and the watch (build.py WEAR_SHARED).
 */
final class BleCandidate {
    /** Told on the main thread. */
    interface Listener {
        /** A phone finished the key exchange: show this SAS with Matches / Cancel. */
        void onSas(String sas);
        /** The SAS went away (that phone left or failed): back to waiting. */
        void onSasGone();
        /** A sealed code arrived (it is released only after Matches). */
        void onCodeIn();
        /** Advertising stopped for good; {@code why} in words. */
        void onEnded(String why);
    }

    static final UUID SERVICE = UUID.fromString(BlePair.SERVICE);
    static final UUID RX = UUID.fromString(BlePair.RX);
    static final UUID TX = UUID.fromString(BlePair.TX);
    private static final long TICK_MS = 1000;

    private final Context ctx;
    private final Listener out;
    private final Handler main = new Handler(Looper.getMainLooper());
    final BlePair.Candidate engine;

    private BluetoothGattServer server;
    private BluetoothLeAdvertiser advertiser;
    private byte[] advertised;
    private String lastSas;
    private boolean codeTold;
    private boolean running;
    /** Prepared (long) writes per connection, bounded by BlePair.MAX_MESSAGE. */
    private final Map<String, ByteArrayOutputStream> prepared = new HashMap<>();

    BleCandidate(Context c, int deviceClass, Listener l) {
        ctx = c.getApplicationContext();
        out = l;
        engine = new BlePair.Candidate(deviceClass, System::currentTimeMillis, new SecureRandom());
    }

    /** The runtime permissions to ask for first (none below Android 12: install-time there). */
    static String[] permissions() {
        if (Build.VERSION.SDK_INT >= 31) {
            return new String[] {Manifest.permission.BLUETOOTH_ADVERTISE, Manifest.permission.BLUETOOTH_CONNECT};
        }
        return new String[0];
    }

    static boolean granted(Context c) {
        for (String p : permissions()) {
            if (c.checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) return false;
        }
        return true;
    }

    /** Why this device cannot advertise right now, or null when it can. */
    static String unavailable(Context c) {
        if (!c.getPackageManager().hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE)) {
            return "This device has no Bluetooth LE.";
        }
        if (!granted(c)) return "Aither needs the Nearby devices permission for this.";
        BluetoothManager bm = c.getSystemService(BluetoothManager.class);
        BluetoothAdapter a = bm == null ? null : bm.getAdapter();
        if (a == null) return "This device has no Bluetooth.";
        if (!a.isEnabled()) return "Turn Bluetooth on, then try again.";
        if (a.getBluetoothLeAdvertiser() == null) return "This device can't be found over Bluetooth.";
        return null;
    }

    /** Open the GATT server, then advertise. False with why in {@code why[0]}. */
    boolean start(String[] why) {
        String no = unavailable(ctx);
        if (no != null) {
            why[0] = no;
            return false;
        }
        try {
            BluetoothManager bm = ctx.getSystemService(BluetoothManager.class);
            advertiser = bm.getAdapter().getBluetoothLeAdvertiser();
            server = bm.openGattServer(ctx, gattCallback);
            if (server == null) {
                why[0] = "Bluetooth didn't open. Try again.";
                return false;
            }
            BluetoothGattService svc = new BluetoothGattService(SERVICE, BluetoothGattService.SERVICE_TYPE_PRIMARY);
            svc.addCharacteristic(new BluetoothGattCharacteristic(RX,
                    BluetoothGattCharacteristic.PROPERTY_WRITE, BluetoothGattCharacteristic.PERMISSION_WRITE));
            svc.addCharacteristic(new BluetoothGattCharacteristic(TX,
                    BluetoothGattCharacteristic.PROPERTY_READ, BluetoothGattCharacteristic.PERMISSION_READ));
            running = true;
            if (!server.addService(svc)) {
                stop();
                why[0] = "Bluetooth didn't open. Try again.";
                return false;
            }
            // advertising starts in onServiceAdded: no phone connects to a server with no service
            main.postDelayed(tick, TICK_MS);
            return true;
        } catch (SecurityException e) {
            stop();
            why[0] = "Aither needs the Nearby devices permission for this.";
            return false;
        }
    }

    /** Stop advertising and close the server. Safe to call twice. */
    void stop() {
        running = false;
        main.removeCallbacks(tick);
        try {
            if (advertiser != null && advertised != null) advertiser.stopAdvertising(advCallback);
        } catch (SecurityException | IllegalStateException e) { /* radio already off */ }
        advertised = null;
        try {
            if (server != null) server.close();
        } catch (SecurityException e) { /* already closed */ }
        server = null;
        synchronized (prepared) { prepared.clear(); }
    }

    /** The owner tapped Matches on {@code sas}; the code (once) when it is also in. */
    String matches(String sas) {
        engine.confirmLocal(sas);
        return engine.takeCode();
    }

    void cancel() {
        engine.cancel();
        stop();
    }

    // ------------------------------------------------------------------ advert

    private void advertise(byte[] data) {
        if (!running || advertiser == null) return;
        try {
            if (advertised != null) advertiser.stopAdvertising(advCallback);
            AdvertiseSettings s = new AdvertiseSettings.Builder()
                    .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                    .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM)
                    .setConnectable(true)
                    // the radio's own box, on top of BlePair.LIFETIME_MS (its max is 180 s)
                    .setTimeout((int) Math.min(180_000, BlePair.LIFETIME_MS))
                    .build();
            AdvertiseData d = new AdvertiseData.Builder()
                    .setIncludeDeviceName(false) // no name, no account: only the service data
                    .setIncludeTxPowerLevel(false)
                    .addServiceData(new ParcelUuid(SERVICE), data)
                    .build();
            advertiser.startAdvertising(s, d, advCallback);
            advertised = data;
        } catch (SecurityException e) {
            end("Aither needs the Nearby devices permission for this.");
        }
    }

    private final AdvertiseCallback advCallback = new AdvertiseCallback() {
        @Override
        public void onStartFailure(int errorCode) {
            main.post(() -> end("This device couldn't start looking (" + errorCode + ")."));
        }
    };

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!running) return;
            byte[] now = engine.advert();
            BlePair.Candidate.Phase ph = engine.phase();
            if (ph == BlePair.Candidate.Phase.CLOSED) {
                end(engine.why().isEmpty() ? "Stopped." : engine.why());
                return;
            }
            if (now == null) {
                // a code is in (or done): no more adverts, the server stays for the read
                if (advertised != null) {
                    try { advertiser.stopAdvertising(advCallback); } catch (SecurityException e) { /* off */ }
                    advertised = null;
                }
            } else if (advertised != null && !Arrays.equals(now, advertised)) {
                advertise(now); // a new key, so a new request id
            }
            tellUi();
            main.postDelayed(this, TICK_MS);
        }
    };

    private void tellUi() {
        String s = engine.sas();
        if (s != null && !s.equals(lastSas)) {
            lastSas = s;
            codeTold = false;
            out.onSas(s);
        } else if (s == null && lastSas != null) {
            lastSas = null;
            codeTold = false;
            out.onSasGone();
        }
        if (!codeTold && engine.phase() == BlePair.Candidate.Phase.CODE) {
            codeTold = true;
            out.onCodeIn();
        }
    }

    private void end(String why) {
        if (!running) return;
        stop();
        out.onEnded(why);
    }

    // ------------------------------------------------------------------ GATT server

    private final BluetoothGattServerCallback gattCallback = new BluetoothGattServerCallback() {
        @Override
        public void onServiceAdded(int status, BluetoothGattService service) {
            main.post(() -> {
                if (!running) return;
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    end("Bluetooth didn't open. Try again.");
                    return;
                }
                byte[] ad = engine.advert();
                if (ad == null) end("Stopped."); else advertise(ad);
            });
        }

        @Override
        public void onConnectionStateChange(BluetoothDevice device, int status, int newState) {
            if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                String who = device.getAddress();
                synchronized (prepared) { prepared.remove(who); }
                engine.peerGone(who);
                main.post(BleCandidate.this::tellUi);
            }
        }

        @Override
        public void onCharacteristicWriteRequest(BluetoothDevice device, int requestId,
                BluetoothGattCharacteristic ch, boolean preparedWrite, boolean responseNeeded,
                int offset, byte[] value) {
            String who = device.getAddress();
            int status = BluetoothGatt.GATT_SUCCESS;
            if (!RX.equals(ch.getUuid()) || value == null) {
                status = BluetoothGatt.GATT_WRITE_NOT_PERMITTED;
            } else if (preparedWrite) {
                synchronized (prepared) {
                    ByteArrayOutputStream b = prepared.get(who);
                    if (b == null) {
                        b = new ByteArrayOutputStream();
                        prepared.put(who, b);
                    }
                    if (offset != b.size() || b.size() + value.length > BlePair.MAX_MESSAGE) {
                        prepared.remove(who);
                        status = BluetoothGatt.GATT_INVALID_OFFSET;
                    } else {
                        b.write(value, 0, value.length);
                    }
                }
            } else if (offset != 0) {
                status = BluetoothGatt.GATT_INVALID_OFFSET;
            } else {
                engine.onWrite(who, value);
                main.post(BleCandidate.this::tellUi);
            }
            respond(device, requestId, status, offset, preparedWrite ? value : null, responseNeeded);
        }

        @Override
        public void onExecuteWrite(BluetoothDevice device, int requestId, boolean execute) {
            String who = device.getAddress();
            ByteArrayOutputStream b;
            synchronized (prepared) { b = prepared.remove(who); }
            if (execute && b != null) {
                engine.onWrite(who, b.toByteArray());
                main.post(BleCandidate.this::tellUi);
            }
            respond(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null, true);
        }

        @Override
        public void onCharacteristicReadRequest(BluetoothDevice device, int requestId, int offset,
                BluetoothGattCharacteristic ch) {
            if (!TX.equals(ch.getUuid())) {
                respond(device, requestId, BluetoothGatt.GATT_READ_NOT_PERMITTED, offset, null, true);
                return;
            }
            byte[] r = engine.reply(device.getAddress());
            if (offset > r.length) {
                respond(device, requestId, BluetoothGatt.GATT_INVALID_OFFSET, offset, null, true);
                return;
            }
            respond(device, requestId, BluetoothGatt.GATT_SUCCESS, offset,
                    Arrays.copyOfRange(r, offset, r.length), true);
        }
    };

    private void respond(BluetoothDevice d, int requestId, int status, int offset, byte[] value, boolean needed) {
        if (!needed) return;
        BluetoothGattServer s = server;
        if (s == null) return;
        try {
            s.sendResponse(d, requestId, status, offset, value);
        } catch (SecurityException e) { /* permission withdrawn mid-way: the phone times out */ }
    }

    // ------------------------------------------------------------------ Identity

    /** Identity's answer to a confirm: status (0 = unreachable) and its JSON. */
    static final class Answer {
        final int status;
        final JSONObject json;

        Answer(int status, JSONObject json) {
            this.status = status;
            this.json = json;
        }
    }

    /**
     * POST {idp}/v1/nodes/pairing/confirm with the code this device received and its own
     * registration. The answer holds the device's token: the caller keeps it in app-private
     * storage and never shows or logs it.
     */
    static Answer confirm(String idp, String code, JSONObject registration) {
        HttpURLConnection c = null;
        try {
            JSONObject body = new JSONObject(registration.toString()).put("code", code);
            c = (HttpURLConnection) new URL(idp + "/v1/nodes/pairing/confirm").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(20000);
            c.setReadTimeout(60000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = c.getOutputStream()) {
                o.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int status = c.getResponseCode();
            JSONObject j = new JSONObject();
            try (InputStream in = status < 400 ? c.getInputStream() : c.getErrorStream()) {
                if (in != null) {
                    ByteArrayOutputStream b = new ByteArrayOutputStream();
                    byte[] buf = new byte[4096];
                    int n;
                    while (b.size() < (1 << 20) && (n = in.read(buf)) > 0) b.write(buf, 0, n);
                    try { j = new JSONObject(b.toString("UTF-8")); } catch (Exception e) { /* not JSON */ }
                }
            }
            return new Answer(status, j);
        } catch (Exception e) {
            return new Answer(0, new JSONObject());
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** The words for a confirm that did not enrol the device. */
    static String refused(int status) {
        if (status == 0) return "No connection. Check Wi-Fi and try again.";
        if (status == 400) return "That code was used or ran out. Start again.";
        if (status == 402) return "Your plan has no room for another device.";
        if (status == 429) return "Too many tries. Wait a few minutes.";
        return "Aither couldn't add this device (" + status + ").";
    }
}
