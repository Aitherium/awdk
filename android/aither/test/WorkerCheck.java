package com.aitherium.aither;

import java.util.List;

/**
 * "Help my fleet" on a desktop JVM (Worker is pure Java): the key is the pinned derivation,
 * the server binds only the tailnet and is never open, -np is 1..4, the worker-on arguments
 * are closed, and the conditions pause it. Exit 0 = all hold. {@code self-test} breaks one
 * rule on purpose and must exit 1.
 */
public class WorkerCheck {
    static int bad;

    static void expect(boolean ok, String what) {
        if (!ok) {
            bad++;
            System.out.println("FAIL " + what);
        }
    }

    public static void main(String[] a) {
        boolean selfTest = a.length > 0 && "self-test".equals(a[0]);
        // the key: the same vector as identity_node_commands.worker_api_key and adk.mesh_worker
        String zeros = "0000000000000000000000000000000000000000000000000000000000000000";
        expect("80b59082e9e54291084e1dbca1485e1cfe5c2046d550079e59e23fc43098032b"
                .equals(Worker.apiKey(zeros, "node-1")), "key vector");
        expect(!Worker.apiKey(zeros, "node-2").equals(Worker.apiKey(zeros, "node-1")), "per device");
        expect(Worker.apiKey("", "node-1").isEmpty(), "no command key, no worker key");
        expect(Worker.apiKey(zeros, "").isEmpty(), "no node id, no worker key");
        String key = Worker.apiKey(zeros, "node-1");
        expect(Worker.fingerprint(key).length() == 8 && !key.startsWith(Worker.fingerprint(key)),
                "the fingerprint is not a prefix of the key");

        // parallel jobs: 1..4, whatever is stored
        expect(Worker.parallel(0) == 1 && Worker.parallel(-3) == 1, "clamp low");
        expect(Worker.parallel(4) == 4 && Worker.parallel(9) == 4, "clamp high");

        // argv: tailnet only, never open, -np/-c follow the setting
        List<String> v = Worker.argv("/lib/libllamaserver.so", "/m.gguf", "100.64.0.52", key, 3, 4);
        expect(v.get(v.indexOf("--host") + 1).equals("100.64.0.52"), "binds the tailnet address");
        expect(v.get(v.indexOf("--api-key") + 1).equals(key), "carries the key");
        expect(v.get(v.indexOf("-np") + 1).equals("3"), "-np");
        expect(v.get(v.indexOf("-c") + 1).equals(String.valueOf(3 * Worker.CTX_PER_SLOT)), "-c per slot");
        expect(v.get(v.indexOf("--port") + 1).equals(String.valueOf(Worker.PORT)), "port");
        expect(v.get(v.indexOf("--alias") + 1).equals("bonsai-1.7b"), "serves as bonsai-1.7b");
        expect(!String.join(" ", Worker.redact(v)).contains(key), "redact hides the key");
        expect(Worker.argv("x", "m", "100.64.0.52", key, 99, 4)
                .get(v.indexOf("-np") + 1).equals("4"), "-np never above 4");
        for (String host : new String[] {"0.0.0.0", "127.0.0.1", "192.168.1.52", "10.0.0.2",
                "100.128.0.1", "", "::"}) {
            boolean refused = false;
            try { Worker.argv("x", "m", host, key, 1, 4); } catch (IllegalArgumentException e) { refused = true; }
            expect(refused, "refuses host " + host);
        }
        boolean open = false;
        try { Worker.argv("x", "m", "100.64.0.52", "", 1, 4); } catch (IllegalArgumentException e) { open = true; }
        expect(open, "refuses to run without a key");

        // worker-on arguments: closed strings only
        for (String p : new String[] {"1", "2", "3", "4"}) expect(Worker.argOk("parallel", p), "parallel " + p);
        for (Object p : new Object[] {"0", "5", "-1", "01", " 1", 2, null}) {
            expect(!Worker.argOk("parallel", p), "parallel refused " + p);
        }
        expect(Worker.argOk("model", "bonsai-1.7b") && Worker.argOk("model", "bonsai-8b"), "models");
        expect(!Worker.argOk("model", "aither-orchestrator") && !Worker.argOk("model", "../x.gguf"),
                "models refused");
        expect(!Worker.argOk("port", "8488") && !Worker.argOk("url", "http://x"), "no other arguments");

        // the conditions: on, a grown-up's phone, keyed, on the mesh, cool, charging + Wi-Fi
        String ip = "100.64.0.52";
        expect(Worker.paused(true, false, ip, key, false, true, true, true).isEmpty(), "serves");
        expect(!Worker.paused(false, false, ip, key, false, true, true, true).isEmpty(), "off");
        expect(!Worker.paused(true, true, ip, key, false, false, true, true).isEmpty(), "child");
        expect(!Worker.paused(true, false, ip, "", false, true, true, true).isEmpty(), "no key");
        expect(!Worker.paused(true, false, "", key, false, true, true, true).isEmpty(), "off the mesh");
        expect(!Worker.paused(true, false, "192.168.1.5", key, false, true, true, true).isEmpty(), "LAN only");
        expect(!Worker.paused(true, false, ip, key, true, false, true, true).isEmpty(), "hot");
        expect(Worker.paused(true, false, ip, key, false, true, false, true).contains("charging"), "unplugged");
        expect(Worker.paused(true, false, ip, key, false, true, true, false).contains("Wi-Fi"), "off Wi-Fi");
        expect(Worker.paused(true, false, ip, key, false, false, false, false).isEmpty(),
                "the owner may switch the guard off");

        if (selfTest) {
            // a host off the tailnet must be noticed
            boolean refused = false;
            try { Worker.argv("x", "m", "100.64.0.52", key, 1, 4); } catch (IllegalArgumentException e) { refused = true; }
            expect(refused, "self-test: deliberately expects a tailnet host to be refused");
        }
        System.out.println(bad == 0 ? "WorkerCheck OK" : bad + " failed");
        System.exit(bad == 0 ? 0 : 1);
    }
}
