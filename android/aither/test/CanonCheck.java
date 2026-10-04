package com.aitherium.aither;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;

/** Driven by check_commands.py: prints one verdict per command line, then a signed result. */
public class CanonCheck {
    public static void main(String[] a) throws Exception {
        String key = a[0], node = a[1];
        long now = Long.parseLong(a[2]);
        for (String line : Files.readAllLines(Paths.get(a[3]), StandardCharsets.UTF_8)) {
            JSONObject c = new JSONObject(line);
            String why = Commands.refuse(c, key, node, now, new ArrayList<>(Arrays.asList("cmd_seen")));
            System.out.println(why.isEmpty() ? "RUN" : "REFUSE " + why);
        }
        JSONObject out = new JSONObject().put("state", "café ☃ 😀 \"q\" \\ /x\u007f\n\u0001")
                .put("n", 3).put("b", true)
                .put("nested", new JSONObject().put("z", 1).put("a", new org.json.JSONArray().put("x").put(JSONObject.NULL)));
        JSONObject r = new JSONObject().put("id", "cmd_1").put("node_id", node).put("ok", true).put("output", out);
        System.out.println("RESULT " + Commands.signResult(key, r));
    }
}
