/* Claude Status — Busy Bar JS app.
 *
 * Polls the Mac-side daemon over the USB network (GET http://10.0.4.21:8765/status)
 * and renders onto the front display via the local HTTP API:
 *   - state ring: pre-rendered .anim assets played natively (smooth)
 *   - model + effort in the Claude Code /effort level color
 *   - ctx progress bar, paired 5h/7d plan usage, state word
 *
 * The .anim assets are uploaded by install_app.py into the canvas app
 * "claude_status" (/ext/apps_assets/claude_status/), which is also the
 * application_name used for all draw calls here.
 *
 * JerryScript environment: fetch({url,method,headers,body}) -> Promise,
 * setInterval/setTimeout, JSON, console.
 */

var DRAW_URL = "http://127.0.0.1/api/display/draw";
var STATUS_URL = "http://10.0.4.21:8765/status";
var APP = "claude_status";
var PRIORITY = 50;

var POLL_MS = 1000;
var TEXT_TIMEOUT = 15;
var ANIM_TIMEOUT = 120;
var ANIM_REFRESH_MS = 60000;
var TEXT_KEEPALIVE_MS = 8000;

var STATE_ANIMS = {
    THINKING: "think.anim", WORKING: "work.anim", WAIT: "wait.anim",
    ERROR: "error.anim", FAILED: "error.anim", COMPLETE: "done.anim",
    IDLE: "idle.anim", OFFLINE: "idle.anim"
};
var STATE_WORDS = {
    THINKING: "THINK", WORKING: "WORK", WAIT: "WAIT", ERROR: "ERR",
    FAILED: "FAIL", COMPLETE: "DONE", IDLE: "IDLE", OFFLINE: "NO LINK"
};
var STATE_COLORS = {
    THINKING: "#AF87FFFF", WORKING: "#FFB000FF", WAIT: "#FF6A00FF",
    ERROR: "#FF2020FF", FAILED: "#FF2020FF", COMPLETE: "#20C040FF",
    IDLE: "#808080FF", OFFLINE: "#666666FF"
};
/* Claude Code theme palette: inactive/permission/warning/fastMode/effortUltra */
var EFFORT_COLORS = {
    low: "#999999FF", medium: "#99CCFFFF", high: "#FFC107FF",
    xhigh: "#FF7814FF", max: "#AF87FFFF"
};

var BAR_X = 50, BAR_Y = 3, BAR_W = 20, BAR_H = 4;
var MODEL_MAX_PX = BAR_X - 2 - 3;

var lastAnimPath = "";
var lastAnimAt = 0;
var lastTexts = "";
var lastTextsAt = 0;

function now() { return Date.now(); }

function draw(elements) {
    return fetch({
        url: DRAW_URL,
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            application_name: APP, priority: PRIORITY, elements: elements
        })
    });
}

function estWidth(text) {
    var narrow = "iljI.,;:' ";
    var wide = "MWmw%";
    var w = 0;
    for (var i = 0; i < text.length; i++) {
        var ch = text.charAt(i);
        if (narrow.indexOf(ch) >= 0) w += 3;
        else if (ch >= "0" && ch <= "9") w += 4;
        else if ((ch >= "A" && ch <= "Z") || wide.indexOf(ch) >= 0) w += 5;
        else w += 4;
    }
    return w;
}

function ctxColor(used) {
    if (used >= 90) return "#FF2020FF";
    if (used >= 80) return "#FF6A00FF";
    if (used >= 50) return "#FFB000FF";
    return "#20C040FF";
}

function planColor(left) {
    if (left <= 10) return "#FF2020FF";
    if (left <= 25) return "#FF6A00FF";
    return "#A0A0A0FF";
}

function textEl(id, x, y, align, text, color) {
    return { id: id, type: "text", display: "front", x: x, y: y, align: align,
             text: text, font: "small", color: color, timeout: TEXT_TIMEOUT };
}

function rectEl(id, x, y, w, h, color) {
    return { id: id, type: "rectangle", display: "front", x: x, y: y,
             width: w, height: h, border_width: 0,
             fill: "solid", fill_colors: [color], timeout: TEXT_TIMEOUT };
}

function infoElements(st) {
    var els = [];
    var state = st.state || "OFFLINE";

    var name = st.model || "";
    var effort = st.effort || "";
    var label = (name + " " + effort).replace(/^\s+|\s+$/g, "");
    while (label.length > 3 && estWidth(label) > MODEL_MAX_PX) {
        name = name.substring(0, name.length - 1);
        label = (name + " " + effort).replace(/^\s+|\s+$/g, "");
    }
    if (label) {
        var mcolor = EFFORT_COLORS[effort] || "#FFFFFFFF";
        els.push(textEl("model", 3, 0, "top_left", label, mcolor));
    }

    els.push(rectEl("ctrack", BAR_X, BAR_Y, BAR_W, BAR_H, "#262626FF"));
    if (typeof st.ctx_used === "number" && st.ctx_used > 0) {
        var fill = Math.round(BAR_W * st.ctx_used / 100);
        if (fill < 1) fill = 1;
        if (fill > BAR_W) fill = BAR_W;
        els.push(rectEl("cfill", BAR_X, BAR_Y, fill, BAR_H, ctxColor(st.ctx_used)));
    }

    var usage = null, worst = 100;
    if (typeof st.five_left === "number" && typeof st.week_left === "number") {
        usage = "5h" + st.five_left + "% 7d" + st.week_left + "%";
        worst = Math.min(st.five_left, st.week_left);
    } else if (typeof st.five_left === "number") {
        usage = "5h" + st.five_left + "%";
        worst = st.five_left;
    } else if (typeof st.week_left === "number") {
        usage = "7d" + st.week_left + "%";
        worst = st.week_left;
    }
    if (usage) els.push(textEl("usage", 3, 15, "bottom_left", usage, planColor(worst)));

    els.push(textEl("state", 69, 15, "bottom_right",
                    STATE_WORDS[state] || state, STATE_COLORS[state] || "#808080FF"));
    return els;
}

function render(st) {
    var state = st.state || "OFFLINE";
    var animPath = STATE_ANIMS[state] || "idle.anim";
    var t = now();

    if (animPath !== lastAnimPath || t - lastAnimAt > ANIM_REFRESH_MS) {
        lastAnimPath = animPath;
        lastAnimAt = t;
        draw([{ id: "ring", type: "animation", display: "front", x: 0, y: 0,
                path: animPath, loop: true, timeout: ANIM_TIMEOUT }]);
    }

    var texts = infoElements(st);
    var key = JSON.stringify(texts);
    if (key !== lastTexts || t - lastTextsAt > TEXT_KEEPALIVE_MS) {
        lastTexts = key;
        lastTextsAt = t;
        draw(texts);
    }
}

function poll() {
    fetch({ url: STATUS_URL, method: "GET" })
        .then(function (resp) { return resp.json(); })
        .then(function (st) { render(st); })
        .catch(function (err) {
            console.log("status fetch failed: " + err);
            render({ state: "OFFLINE" });
        });
}

console.log("Claude Status app started");
poll();
setInterval(poll, POLL_MS);
