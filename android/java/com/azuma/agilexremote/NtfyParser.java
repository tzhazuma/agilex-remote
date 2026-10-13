package com.azuma.agilexremote;

/**
 * Parsing helpers for the robot's ntfy.sh heartbeat.
 *
 * Kept free of Android imports so it can be unit-tested on a plain JVM
 * (see test/NtfyParserTest.java).
 *
 * The robot publishes newline-delimited JSON, e.g.:
 * {"id":"...","time":1791878286,"event":"message","topic":"YOUR-NTFY-TOPIC-HERE",
 *  "title":"agilex robot","message":"ROBOT_IP","tags":["robot"]}
 *
 * NOTE the trap: the line contains BOTH {@code "event":"message"} and
 * {@code "message":"ROBOT_IP"}. A naive search for the word "message"
 * matches the event value first and returns "topic". Everything here therefore
 * matches the literal KEY {@code "message":"}.
 */
final class NtfyParser {

    private static final String MESSAGE_KEY = "\"message\":\"";

    private NtfyParser() {
    }

    /**
     * Value of the LAST {@code "message":"..."} field in one ndjson line, or null
     * if the line carries no message field.
     */
    static String lastMessageField(String line) {
        if (line == null) {
            return null;
        }
        int idx = line.lastIndexOf(MESSAGE_KEY);
        if (idx < 0) {
            return null;
        }
        int start = idx + MESSAGE_KEY.length();
        int end = line.indexOf('"', start);
        if (end < 0) {
            return null;
        }
        return line.substring(start, end);
    }

    /** Value of the last message field across a whole ndjson document, or null. */
    static String lastMessage(String ndjson) {
        if (ndjson == null) {
            return null;
        }
        String last = null;
        for (String line : ndjson.split("\n")) {
            String m = lastMessageField(line);
            if (m != null) {
                last = m;
            }
        }
        return last;
    }

    /** Strict dotted-quad check. */
    static boolean isIpv4(String s) {
        if (s == null) {
            return false;
        }
        String[] parts = s.trim().split("\\.");
        if (parts.length != 4) {
            return false;
        }
        for (String p : parts) {
            if (p.isEmpty() || p.length() > 3) {
                return false;
            }
            for (int i = 0; i < p.length(); i++) {
                if (!Character.isDigit(p.charAt(i))) {
                    return false;
                }
            }
            if (Integer.parseInt(p) > 255) {
                return false;
            }
        }
        return true;
    }

    /** Full URL for an IPv4 robot address on the controller port. */
    static String urlFor(String ip, int port) {
        return "http://" + ip.trim() + ":" + port + "/";
    }
}
