package com.azuma.agilexremote;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.text.TextUtils;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.CookieManager;
import android.webkit.ConsoleMessage;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Thin WebView shell around the AgileX robot's remote controller page
 * (remote/robot-files/remote-ui.html, served by agilex-remote-server.py).
 *
 * The controller UI itself is NOT reimplemented here. This app provides:
 *   - a full-screen WebView (JS + DOM storage + cookies, cleartext HTTP allowed)
 *   - a Reload / Settings toolbar
 *   - a persisted, editable base URL
 *   - AUTO-DISCOVERY of the robot's current IP from its ntfy.sh heartbeat, because
 *     the robot is on DHCP and its address changes on reboot
 *   - a settings/error screen so a failed load never shows a blank page
 */
public class MainActivity extends Activity {

    private static final String TAG = "AgileXRemote";

    private static final String PREFS = "agilex_remote";
    private static final String KEY_BASE_URL = "base_url";
    private static final String KEY_NTFY_TOPIC = "ntfy_topic";

    /** Default target: the robot's own LAN address (the phone cannot use the Mac's tunnel). */
    private static final String DEFAULT_BASE_URL = "http://ROBOT_IP:8765/";
    private static final String DEFAULT_NTFY_TOPIC = "YOUR-NTFY-TOPIC-HERE";
    private static final int HTTP_TIMEOUT_MS = 8000;
    private static final int PORT = 8765;

    private WebView web;
    private ScrollView settingsPanel;
    private EditText urlField;
    private EditText topicField;
    private TextView statusView;
    private TextView urlLabel;

    /** Guard so a failing URL cannot trigger endless discovery loops. */
    private boolean autoDiscoveryDone = false;

    /** True while the panel shows an error/progress state, so a page load may dismiss it.
     *  A panel the user opened deliberately must NOT be closed by a background reload. */
    private boolean settingsIsError = false;
    private String pendingUrl = null;

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                getResources().getDisplayMetrics());
    }

    // ------------------------------------------------------------------ prefs

    private String pref(String key, String def) {
        String v = getSharedPreferences(PREFS, MODE_PRIVATE).getString(key, def);
        return TextUtils.isEmpty(v) ? def : v.trim();
    }

    private String baseUrl() {
        return pref(KEY_BASE_URL, DEFAULT_BASE_URL);
    }

    private String ntfyTopic() {
        return pref(KEY_NTFY_TOPIC, DEFAULT_NTFY_TOPIC);
    }

    private void put(String key, String value) {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(key, value).apply();
    }

    /** Accepts "ROBOT_IP:8765" as well as a full URL. */
    private static String normalize(String raw) {
        String u = (raw == null) ? "" : raw.trim();
        if (u.isEmpty()) {
            return DEFAULT_BASE_URL;
        }
        if (!u.startsWith("http://") && !u.startsWith("https://")) {
            u = "http://" + u;
        }
        return u;
    }

    // ------------------------------------------------------------------ lifecycle

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#0E1620"));
        // Keep the toolbar clear of the status bar: without this the Reload/Settings
        // buttons sit under the clock/battery strip and taps there go to the system
        // (observed on the test device - SETTINGS was unreachable).
        root.setFitsSystemWindows(true);

        // ---------------- toolbar ----------------
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(Color.parseColor("#1B2430"));
        bar.setPadding(dp(10), dp(4), dp(10), dp(4));

        urlLabel = new TextView(this);
        urlLabel.setTextColor(Color.parseColor("#8FB7D9"));
        urlLabel.setTextSize(11);
        urlLabel.setSingleLine(true);
        urlLabel.setEllipsize(TextUtils.TruncateAt.MIDDLE);
        bar.addView(urlLabel, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        Button reloadBtn = new Button(this);
        reloadBtn.setText(R.string.reload);
        reloadBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                web.reload();
            }
        });
        bar.addView(reloadBtn);

        Button settingsBtn = new Button(this);
        settingsBtn.setText(R.string.settings);
        settingsBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showSettings(null);
            }
        });
        bar.addView(settingsBtn);

        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        // ---------------- content ----------------
        FrameLayout content = new FrameLayout(this);
        root.addView(content, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        // The robot's server hands out an access cookie on GET /; off-loopback API
        // calls without it get HTTP 403, so cookies must be enabled.
        final CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);

        web = new WebView(this);
        cm.setAcceptThirdPartyCookies(web, true);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        web.setBackgroundColor(Color.parseColor("#0E1620"));

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage m) {
                Log.i(TAG, "console: " + m.message() + " @" + m.sourceId() + ":" + m.lineNumber());
                return true;
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return false; // keep all navigation inside the WebView
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                Log.i(TAG, "onPageFinished: " + url);
                statusView.setText("");
                if (settingsIsError) {          // only auto-close an error screen
                    settingsIsError = false;
                    settingsPanel.setVisibility(View.GONE);
                }
                if (pendingUrl != null) {
                    String origin = originOf(pendingUrl);
                    if (origin != null) {
                        put(KEY_BASE_URL, origin);   // remember what actually worked
                        urlLabel.setText(origin);
                    }
                    pendingUrl = null;
                }
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (r == null || r.isForMainFrame()) {
                    CharSequence d = (e == null) ? "unknown error" : e.getDescription();
                    Log.w(TAG, "main-frame load error: " + d);
                    onLoadFailed("Could not load:\n" + baseUrl() + "\n\n" + d);
                }
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView v, int code, String desc, String failingUrl) {
                onLoadFailed("Could not load:\n" + failingUrl + "\n\n" + desc);
            }
        });

        content.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        settingsPanel = buildSettingsPanel();
        settingsPanel.setVisibility(View.GONE);
        content.addView(settingsPanel, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);
        loadCurrent();
    }

    /** A failed main-frame load triggers discovery once, then falls back to the settings screen. */
    private void onLoadFailed(String message) {
        if (!autoDiscoveryDone) {
            autoDiscoveryDone = true;
            showSettings(message + "\n\nLooking for the robot automatically\u2026");
            discoverRobot();
        } else {
            showSettings(message);
        }
    }

    // ------------------------------------------------------------------ settings UI

    /** Settings screen - also doubles as the error screen so a failed load never shows a blank page. */
    private ScrollView buildSettingsPanel() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.parseColor("#0E1620"));

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(18), dp(18), dp(18), dp(18));
        scroll.addView(col, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title = new TextView(this);
        title.setText(R.string.settings_title);
        title.setTextColor(Color.parseColor("#E6F1FA"));
        title.setTextSize(20);
        col.addView(title);

        TextView help = new TextView(this);
        help.setText(R.string.settings_help);
        help.setTextColor(Color.parseColor("#9FB6C6"));
        help.setTextSize(13);
        help.setPadding(0, dp(8), 0, dp(14));
        col.addView(help);

        TextView label = new TextView(this);
        label.setText(R.string.base_url_label);
        label.setTextColor(Color.parseColor("#8FB7D9"));
        label.setTextSize(12);
        col.addView(label);

        urlField = new EditText(this);
        urlField.setSingleLine(true);
        urlField.setTextSize(14);
        urlField.setTextColor(Color.parseColor("#E6F1FA"));
        urlField.setHint(R.string.base_url_hint);
        urlField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        urlField.setImeOptions(EditorInfo.IME_ACTION_GO);
        urlField.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override
            public boolean onEditorAction(TextView v, int actionId, KeyEvent event) {
                if (actionId == EditorInfo.IME_ACTION_GO) {
                    saveAndLoad();
                    return true;
                }
                return false;
            }
        });
        col.addView(urlField, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button saveBtn = new Button(this);
        saveBtn.setText(R.string.save_and_load);
        saveBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                saveAndLoad();
            }
        });
        col.addView(saveBtn, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        Button defaultBtn = new Button(this);
        defaultBtn.setText(R.string.use_default);
        defaultBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                urlField.setText(DEFAULT_BASE_URL);
            }
        });
        row.addView(defaultBtn, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        Button cancelBtn = new Button(this);
        cancelBtn.setText(R.string.cancel);
        cancelBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                settingsPanel.setVisibility(View.GONE);
            }
        });
        row.addView(cancelBtn, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        col.addView(row, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        // ---- auto-discovery section ----
        TextView div = new TextView(this);
        div.setText(R.string.discovery_title);
        div.setTextColor(Color.parseColor("#E6F1FA"));
        div.setTextSize(16);
        div.setPadding(0, dp(22), 0, dp(6));
        col.addView(div);

        TextView dhelp = new TextView(this);
        dhelp.setText(R.string.discovery_help);
        dhelp.setTextColor(Color.parseColor("#9FB6C6"));
        dhelp.setTextSize(13);
        dhelp.setPadding(0, 0, 0, dp(10));
        col.addView(dhelp);

        TextView tlabel = new TextView(this);
        tlabel.setText(R.string.topic_label);
        tlabel.setTextColor(Color.parseColor("#8FB7D9"));
        tlabel.setTextSize(12);
        col.addView(tlabel);

        topicField = new EditText(this);
        topicField.setSingleLine(true);
        topicField.setTextSize(14);
        topicField.setTextColor(Color.parseColor("#E6F1FA"));
        topicField.setHint(DEFAULT_NTFY_TOPIC);
        topicField.setInputType(InputType.TYPE_CLASS_TEXT);
        col.addView(topicField, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button findBtn = new Button(this);
        findBtn.setText(R.string.find_robot);
        findBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                put(KEY_NTFY_TOPIC, topicField.getText().toString().trim());
                discoverRobot();
            }
        });
        col.addView(findBtn, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        statusView = new TextView(this);
        statusView.setTextColor(Color.parseColor("#FF8A80"));
        statusView.setTextSize(12);
        statusView.setPadding(0, dp(12), 0, 0);
        col.addView(statusView);

        TextView cur = new TextView(this);
        cur.setTextColor(Color.parseColor("#5C7488"));
        cur.setTextSize(11);
        cur.setPadding(0, dp(14), 0, 0);
        cur.setText("Default URL: " + DEFAULT_BASE_URL
                + "\nDefault topic: " + DEFAULT_NTFY_TOPIC
                + "\nPort: " + PORT);
        col.addView(cur);

        return scroll;
    }

    private void showSettings(String message) {
        urlField.setText(baseUrl());
        topicField.setText(ntfyTopic());
        settingsIsError = (message != null);   // manual open (null) survives page loads
        if (message != null) {
            statusView.setText(message);
        }
        settingsPanel.setVisibility(View.VISIBLE);
    }

    private void saveAndLoad() {
        put(KEY_BASE_URL, normalize(urlField.getText().toString()));
        put(KEY_NTFY_TOPIC, topicField.getText().toString().trim());
        statusView.setText("");
        settingsPanel.setVisibility(View.GONE);
        loadCurrent();
    }

    private void loadCurrent() {
        String url = baseUrl();
        urlLabel.setText(url);
        pendingUrl = url;
        Log.i(TAG, "loadUrl: " + url);
        web.loadUrl(url);
    }

    // ------------------------------------------------------------------ discovery

    /**
     * Resolve the robot's current IP from its ntfy.sh heartbeat and load it.
     * The robot publishes its IP on boot and every 5 minutes, so this survives DHCP changes.
     */
    private void discoverRobot() {
        final String topic = ntfyTopic();
        final String endpoint = "https://ntfy.sh/" + topic + "/json?poll=1";
        statusView.setText("Looking for the robot\u2026\n" + endpoint);
        settingsIsError = true;
        settingsPanel.setVisibility(View.VISIBLE);
        Log.i(TAG, "discovery: GET " + endpoint);

        new Thread(new Runnable() {
            @Override
            public void run() {
                String ip = null;
                String err = null;
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(endpoint).openConnection();
                    conn.setConnectTimeout(HTTP_TIMEOUT_MS);
                    conn.setReadTimeout(HTTP_TIMEOUT_MS);
                    conn.setRequestProperty("Accept", "application/x-ndjson");
                    int code = conn.getResponseCode();
                    if (code != 200) {
                        err = "ntfy.sh returned HTTP " + code;
                    } else {
                        BufferedReader r = new BufferedReader(
                                new InputStreamReader(conn.getInputStream(), "UTF-8"));
                        String line;
                        String last = null;
                        while ((line = r.readLine()) != null) {
                            String m = NtfyParser.lastMessageField(line);
                            if (m != null) {
                                last = m;
                            }
                        }
                        r.close();
                        if (last == null) {
                            err = "no IP published on topic \"" + topic + "\" yet";
                        } else if (!NtfyParser.isIpv4(last)) {
                            err = "unexpected payload: \"" + last + "\"";
                        } else {
                            ip = last.trim();
                        }
                    }
                } catch (Exception e) {
                    err = e.getClass().getSimpleName() + ": " + e.getMessage();
                } finally {
                    if (conn != null) {
                        conn.disconnect();
                    }
                }

                final String foundIp = ip;
                final String failure = err;
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (foundIp != null) {
                            String url = "http://" + foundIp + ":" + PORT + "/";
                            Log.i(TAG, "discovery: found " + foundIp);
                            put(KEY_BASE_URL, url);
                            urlField.setText(url);
                            statusView.setText("Found robot at " + foundIp);
                            settingsPanel.setVisibility(View.GONE);
                            pendingUrl = url;
                            urlLabel.setText(url);
                            web.loadUrl(url);
                        } else {
                            Log.w(TAG, "discovery failed: " + failure);
                            statusView.setText("Could not find the robot.\n" + failure
                                    + "\n\nCheck the phone has internet, then retry."
                                    + "\nYou can also type the URL manually above.");
                        }
                    }
                });
            }
        }).start();
    }

    /** scheme://host:port/ from a URL, or null. */
    private static String originOf(String url) {
        if (url == null) {
            return null;
        }
        try {
            URL u = new URL(url);
            int port = (u.getPort() > 0) ? u.getPort() : 80;
            return u.getProtocol() + "://" + u.getHost() + ":" + port + "/";
        } catch (Exception e) {
            return null;
        }
    }

    // ------------------------------------------------------------------ misc

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (settingsPanel.getVisibility() == View.VISIBLE) {
                settingsPanel.setVisibility(View.GONE);
                return true;
            }
            if (web.canGoBack()) {
                web.goBack();
                return true;
            }
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }
}
