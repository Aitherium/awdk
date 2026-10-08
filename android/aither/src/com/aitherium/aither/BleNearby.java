package com.aitherium.aither;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The phone's "Nearby devices" list, as pure Java so test/BlePairCheck.java runs it: which
 * scanned adverts are shown, for how long, and what a flood of adverts can and cannot do.
 *
 * <ul>
 * <li>Only a well-formed advert (BlePair.Advert.decode) is listed; nothing in it is shown
 *     but its kind ("A watch") and how close it is.</li>
 * <li>An entry not heard for {@link #STALE_MS} drops off (its id rotated or it stopped).</li>
 * <li>At most {@link #MAX} entries; a stronger newcomer replaces the weakest.</li>
 * <li>At most {@link #NEW_PER_WINDOW} new ids per {@link #WINDOW_MS}: past it, new ids are
 *     ignored for a window and {@link #flooded} says so, so a radio spraying fresh ids cannot
 *     bury the real device or grow the list.</li>
 * <li>Too faint to be in the room ({@link #MIN_RSSI}) is not listed.</li>
 * </ul>
 */
final class BleNearby {
    static final int MAX = 8;
    static final long STALE_MS = 15_000;
    static final int NEW_PER_WINDOW = 12;
    static final long WINDOW_MS = 10_000;
    static final int MIN_RSSI = -90;
    /** How long the phone scans per tap of "Look again": never in the background. */
    static final long SCAN_MS = 60_000;

    static final class Device {
        final String rid;
        final BlePair.Advert advert;
        int rssi;
        long seen;
        Object handle; // the radio's own object (BluetoothDevice); never read here

        Device(BlePair.Advert advert, int rssi, long seen, Object handle) {
            this.rid = advert.ridHex();
            this.advert = advert;
            this.rssi = rssi;
            this.seen = seen;
            this.handle = handle;
        }

        String label() {
            return BlePair.label(advert.deviceClass) + " · " + closeness(rssi);
        }
    }

    private final Map<String, Device> byRid = new LinkedHashMap<>();
    private long windowStart = Long.MIN_VALUE;
    private int newInWindow;
    private long floodedUntil = Long.MIN_VALUE;

    /** One scan result. True when the list changed (a new device, or one dropped for it). */
    synchronized boolean seen(byte[] serviceData, int rssi, Object handle, long now) {
        prune(now);
        BlePair.Advert ad = BlePair.Advert.decode(serviceData);
        if (ad == null || rssi < MIN_RSSI) return false;
        String rid = ad.ridHex();
        Device d = byRid.get(rid);
        if (d != null) {
            d.rssi = rssi;
            d.seen = now;
            d.handle = handle;
            return false;
        }
        if (windowStart == Long.MIN_VALUE || now - windowStart >= WINDOW_MS) {
            windowStart = now;
            newInWindow = 0;
        }
        if (now < floodedUntil) return false;
        if (++newInWindow > NEW_PER_WINDOW) {
            floodedUntil = now + WINDOW_MS;
            return false;
        }
        if (byRid.size() >= MAX) {
            Device weakest = null;
            for (Device x : byRid.values()) if (weakest == null || x.rssi < weakest.rssi) weakest = x;
            if (weakest == null || weakest.rssi >= rssi) return false;
            byRid.remove(weakest.rid);
        }
        byRid.put(rid, new Device(ad, rssi, now, handle));
        return true;
    }

    private void prune(long now) {
        for (Iterator<Device> it = byRid.values().iterator(); it.hasNext(); ) {
            if (now - it.next().seen > STALE_MS) it.remove();
        }
    }

    /** What to show, closest first. */
    synchronized List<Device> list(long now) {
        prune(now);
        List<Device> out = new ArrayList<>(byRid.values());
        Collections.sort(out, (a, b) -> Integer.compare(b.rssi, a.rssi));
        return out;
    }

    synchronized Device get(String rid, long now) {
        prune(now);
        return byRid.get(rid);
    }

    /** True while new ids are being ignored because too many arrived at once. */
    synchronized boolean flooded(long now) {
        return now < floodedUntil;
    }

    synchronized void clear() {
        byRid.clear();
    }

    static String closeness(int rssi) {
        if (rssi >= -55) return "right here";
        if (rssi >= -70) return "close";
        return "in range";
    }
}
