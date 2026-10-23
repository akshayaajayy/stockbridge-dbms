/*
 * StockBridge floating AI chatbot.
 * Loaded on every page (except /assistant) from base.html.
 * Uses helpers defined in base.html: post(), esc().
 * Talks to POST /api/ai/chat. Conversation survives page changes
 * (kept in sessionStorage for the current browser tab).
 */
(function () {

    if (document.getElementById("cb-root")) return;

    var KEY = "sb_chat_v1";
    var MAX_MSGS = 24;
    var MAX_ROWS = 20;

    var SUGGESTIONS = [
        "Which branch has the highest inventory?",
        "Show me products with aging stock",
        "Which pending transfer has the highest potential saving?",
        "How many purchases have been approved?"
    ];

    var state = load();
    var busy = false;
    var configured = null;

    function load() {
        try {
            var v = JSON.parse(sessionStorage.getItem(KEY));
            if (v && Array.isArray(v.msgs)) return v;
        } catch (e) { /* storage unavailable */ }
        return { open: false, msgs: [] };
    }

    function save() {
        try {
            state.msgs = state.msgs.slice(-MAX_MSGS);
            sessionStorage.setItem(KEY, JSON.stringify(state));
        } catch (e) { /* ignore */ }
    }

    /* ---------- build the widget ---------- */

    var root = document.createElement("div");
    root.id = "cb-root";
    root.innerHTML =
        '<button id="cb-launch" type="button" aria-label="Open AI chat" aria-expanded="false">' +
            '<span class="cb-spark" aria-hidden="true">✦</span><span class="cb-launch-text">Ask AI</span>' +
        '</button>' +
        '<section id="cb-panel" role="dialog" aria-label="AI assistant" hidden>' +
            '<header class="cb-head">' +
                '<div><b>StockBridge AI</b><small>Ask about your stock</small></div>' +
                '<div class="cb-head-actions">' +
                    '<button type="button" id="cb-clear" title="Clear conversation" aria-label="Clear conversation">⟲</button>' +
                    '<a href="/assistant" title="Open full page" aria-label="Open full page">⤢</a>' +
                    '<button type="button" id="cb-close" aria-label="Close chat">×</button>' +
                '</div>' +
            '</header>' +
            '<div id="cb-notice" class="cb-notice" hidden></div>' +
            '<div id="cb-log" class="cb-log" aria-live="polite"></div>' +
            '<form id="cb-form" class="cb-form" autocomplete="off">' +
                '<input id="cb-input" type="text" maxlength="500" placeholder="Ask about your inventory..." aria-label="Your question">' +
                '<button id="cb-send" type="submit">Send</button>' +
            '</form>' +
        '</section>';

    document.body.appendChild(root);

    var launch = document.getElementById("cb-launch");
    var panel = document.getElementById("cb-panel");
    var log = document.getElementById("cb-log");
    var form = document.getElementById("cb-form");
    var input = document.getElementById("cb-input");
    var sendBtn = document.getElementById("cb-send");
    var notice = document.getElementById("cb-notice");

    /* ---------- rendering ---------- */

    function fmt(v) {
        if (v === null || v === undefined) return "-";
        if (typeof v === "number") {
            return Number.isInteger(v)
                ? v.toLocaleString("en-IN")
                : v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
        }
        return esc(v);
    }

    function msgHtml(m) {

        if (m.role === "user") {
            return '<div class="cb-msg user"><div class="cb-bubble">' + esc(m.text) + '</div></div>';
        }

        var html = '<div class="cb-bubble' + (m.error ? " err" : "") + '"><p>' + esc(m.text) + '</p>';

        if (m.columns && m.columns.length && m.rows && m.rows.length) {

            html += '<div class="cb-table"><table><thead><tr>' +
                m.columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") +
                "</tr></thead><tbody>" +
                m.rows.map(function (r) {
                    return "<tr>" + r.map(function (v) { return "<td>" + fmt(v) + "</td>"; }).join("") + "</tr>";
                }).join("") +
                "</tbody></table></div>";

            if (m.total > m.rows.length) {
                html += '<p class="cb-muted">Showing ' + m.rows.length + " of " + m.total +
                    ' rows. <a href="/assistant">Open full page</a> for more.</p>';
            }
        }

        if (m.sql) {
            html += '<details class="cb-sql"><summary>Query used</summary><pre>' + esc(m.sql) + "</pre></details>";
        }

        return '<div class="cb-msg bot">' + html + "</div></div>";
    }

    function render() {

        var html = "";

        if (state.msgs.length === 0) {
            html += '<div class="cb-msg bot"><div class="cb-bubble"><p>Hi! Ask me anything about your stock, branches, aging items or transfers.</p>' +
                '<div class="cb-chips">' +
                SUGGESTIONS.map(function (s) {
                    return '<button type="button" class="cb-chip">' + esc(s) + "</button>";
                }).join("") +
                "</div></div></div>";
        }

        state.msgs.forEach(function (m) { html += msgHtml(m); });

        if (busy) {
            html += '<div class="cb-msg bot"><div class="cb-bubble"><span class="cb-muted">Thinking...</span></div></div>';
        }

        log.innerHTML = html;
        log.scrollTop = log.scrollHeight;
    }

    function setOpen(open) {

        state.open = open;
        panel.hidden = !open;
        launch.hidden = open;
        launch.setAttribute("aria-expanded", String(open));
        save();

        if (open) {
            render();
            checkStatus();
            input.focus();
        } else {
            launch.focus();
        }
    }

    function checkStatus() {

        if (configured !== null) return;

        fetch("/api/ai/status")
            .then(function (r) { return r.json(); })
            .then(function (s) {
                configured = !!s.configured && s.allowed !== false;
                if (s.allowed === false) {
                    notice.hidden = false;
                    notice.textContent = "The AI assistant is available to admins and managers.";
                    sendBtn.disabled = true;
                } else if (!configured) {
                    notice.hidden = false;
                    notice.textContent = s.problem || "The AI isn't set up yet. Run python setup_ai.py.";
                    sendBtn.disabled = true;
                }
            })
            .catch(function () { /* leave enabled; errors show on send */ });
    }

    /* ---------- asking ---------- */

    function ask(question) {

        question = (question || "").trim();

        if (busy || !question || configured === false) return;

        var history = state.msgs
            .filter(function (m) { return m.role === "user"; })
            .slice(-4)
            .map(function (m) { return { q: m.text }; });

        state.msgs.push({ role: "user", text: question });
        busy = true;
        sendBtn.disabled = true;
        input.value = "";
        save();
        render();

        post("/api/ai/chat", { question: question, history: history })
            .then(function (d) {

                if (!d.success) {
                    state.msgs.push({ role: "bot", text: d.message || "Something went wrong.", error: true });
                    return;
                }

                state.msgs.push({
                    role: "bot",
                    error: !!d.blocked,
                    text: d.answer,
                    sql: d.sql,
                    columns: d.columns,
                    rows: (d.rows || []).slice(0, MAX_ROWS),
                    total: (d.rows || []).length
                });
            })
            .catch(function () {
                state.msgs.push({ role: "bot", text: "Could not reach the server. Try again.", error: true });
            })
            .then(function () {
                busy = false;
                sendBtn.disabled = configured === false;
                save();
                render();
                input.focus();
            });
    }

    /* ---------- events ---------- */

    launch.addEventListener("click", function () { setOpen(true); });
    document.getElementById("cb-close").addEventListener("click", function () { setOpen(false); });

    document.getElementById("cb-clear").addEventListener("click", function () {
        state.msgs = [];
        save();
        render();
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        ask(input.value);
    });

    log.addEventListener("click", function (e) {
        var chip = e.target.closest(".cb-chip");
        if (chip) ask(chip.textContent);
    });

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && state.open) setOpen(false);
    });

    /* restore previous state */
    if (state.open) {
        panel.hidden = false;
        launch.hidden = true;
        launch.setAttribute("aria-expanded", "true");
        render();
        checkStatus();
    }

})();
