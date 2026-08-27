# Extending busybar-claude-status

Two extension axes: **more agents** (Codex, Cursor, anything) via the
reporting protocol, and **more links to the device** via transports.

```
claude-code (built-in adapter) --.
codex / cursor / your script ----+--> POST /v1/report --> SessionStore --> Renderer --> Transport
                                      (the standard)      (normalized)    (core logic) (usb/wifi/cloud/ble)
```

The display core only ever sees the normalized schema below. Provider
quirks (Claude's `/effort` palette colors, its 5h/7d plan windows, its
statusline JSON) live in adapters and never reach the renderer.

---

## 1. Reporting protocol (v1)

Anything that can run one HTTP request can drive the display:

```bash
curl -X POST http://127.0.0.1:8765/v1/report -H 'Content-Type: application/json' -d '{
  "source":     "codex",
  "session_id": "abc123",
  "state":      "WORKING",
  "label":      "GPT5 codex",
  "label_color":"#99CCFF",
  "context_pct": 63,
  "quotas":     [{"name":"wk","left_pct":42}]
}'
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source` | string ≤32 | yes | tool id, e.g. `codex`, `cursor` |
| `session_id` | string ≤128 | yes | unique within the source |
| `state` | enum | no | `THINKING WORKING WAIT ERROR FAILED COMPLETE IDLE` — drives the ring animation + state word; omit to update data only |
| `label` | string ≤64 | no | line-1 text (tool/model name); ASCII; auto-trimmed to fit |
| `label_color` | `#RRGGBB[AA]` | no | line-1 color (default white) |
| `context_pct` | 0–100 | no | fills the progress bar (green→yellow→orange→red) |
| `quotas` | array | no | up to 2 rendered as `name+left%` (e.g. `5h85% 7d97%`); each `{name ≤6, left_pct 0-100, resets_at?: unix_s}` — after `resets_at` passes, shown as 100% left |
| `badges` | array of names | no | small glyphs after the label; known: `fast` (lightning bolt). Unknown names are ignored, so reporting a badge is always safe |
| `ttl_s` | seconds | no | session forgotten after this silence (default 6h) |
| `ended` | bool | no | `true` removes the session immediately |

Semantics:

- Reports **merge**: send `state` from lifecycle hooks and data fields
  from wherever you compute them, at different rates.
- With several sessions/tools reporting, the **most recently active one
  owns the display** (`GET /status` shows which, including `source`).
- Every field except `source`/`session_id` degrades gracefully when
  missing — a state-only reporter still gets the ring + state word.
- `COMPLETE` auto-decays to `IDLE` after 30 s; `IDLE` releases the
  screen after 10 min.

`GET /status` returns the same normalized shape (what the renderer and
the on-device app consume); `GET /health` lists all live sessions.

### Writing an adapter

An adapter is just "run curl at the right moments":

- **Claude Code** (built-in): statusline command forwards its JSON to
  `/statusline`, hooks post `/state?state=X` — the daemon maps both onto
  the normalized schema (`claude_statusline_report()` in `daemon.py`).
  Claude's specialness — effort→color from the CLI's own palette, the
  model-follows-plan 5h/7d windows — is entirely inside that function.
- **Codex CLI**: shipped — `adapters/codex_status.py`. Zero-config: it
  reads `~/.codex/config.toml` and the newest session rollout, deriving
  everything generically so model renames never break it: the label is
  prettified from the raw id (`gpt-5.6-sol` → `5.6 Sol` + effort),
  `service_tier` ≠ default becomes a badge (`fast` → lightning),
  context % comes from `last_token_usage / model_context_window`, and
  quotas from Codex's own `rate_limits` (names derived from
  `window_minutes`: 10080 → `7d`). Run it alongside the daemon:
  `python3 adapters/codex_status.py` — or better, make it
  **auto-start**: `python3 adapters/install_codex_autostart.py install`
  wires Codex's `notify` hook to `adapters/codex_notify.sh`, which
  chains your previous notifier (preserved verbatim), keeps the daemon +
  adapter alive on every Codex turn, and pushes the turn's end state
  instantly. `uninstall` restores everything. (Codex *skills* are
  model-invoked instruction packages and *plugins* are connector
  manifests — neither can run a background service, so the notify hook
  is the native auto-start point.)
- **Cursor**: use Cursor Hooks (`hooks.json`, e.g. `beforeShellExecution`
  / `stop`) to post `WORKING` / `COMPLETE` with
  `label: "Cursor"`, or wrap `cursor-agent` invocations.
- **Anything else** (CI, long scripts): `trap` + curl gets you a state
  lamp in three lines of shell.

Keep one stable `session_id` per logical session so merging works.

---

## 2. Transports

Selected with `BUSYBAR_TRANSPORT` (default `usb`):

| Transport | Config | Notes |
| --- | --- | --- |
| `usb` | none | `http://10.0.4.20/api`, no auth, lowest latency |
| `wifi` | `BUSYBAR_HOST` (device LAN IP), `BUSYBAR_TOKEN` (password) | enable Wi-Fi access + set the password in the device web UI (`http://10.0.4.20` → Network); token goes in the `x-api-token` header |
| `cloud` | `BUSYBAR_TOKEN` (API token from the BUSY account) | `https://api.busy.app/busybar`, `Authorization: Bearer`; works anywhere, highest latency |
| `ble` | — | designed below, not implemented |

All transports speak the same HTTP API, so the `.anim` assets, the
theme, and every field note in the README apply unchanged. Note for
`wifi`/`cloud`: the on-device JS app polls the daemon at the USB host
address (`10.0.4.21`) — over other links, adjust `STATUS_URL` in
`device_app/scripts/main.js` to an address your device can reach.

### BLE transport design (future work)

The firmware tunnels **raw HTTP/1.1 over BLE** to its loopback web
server (`applications/services/ble/http/ble_http_repeater.c`), framed
over a Nordic UART Service. Verified against firmware source:

- Service `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`
  - RX (write)  `6E400002-…` — central → device bytes
  - TX (notify) `6E400003-…` — device → central bytes
  - CNT `6E400004-…` — **session counter**: the repeater publishes a
    request number after each connection to the loopback server closes;
    writing `0` forces a session reset on the device side
- Enable BLE via `POST /api/ble/enable`, pair via `/api/ble/pairing` +
  on-device confirmation (bonding required; forget-pairing was crashy
  before 1.2.x).
- Protocol per request: serialize a full HTTP/1.1 request
  (`Content-Length` framing, `Connection: keep-alive` semantics are
  managed by the repeater), write it to RX in MTU-sized chunks, then
  reassemble TX notifications until the response's `Content-Length` is
  satisfied. One request in flight at a time; on a 4 s TX-confirm stall
  the device resets the session — resubscribe, sync CNT, retry.
- Implementation sketch (Python): `bleak` (the one optional dependency),
  a `BleHttpTransport(HttpTransport)` whose `_request` routes through the
  tunnel instead of a socket; reconnect/backoff loop; serialize with a
  lock. Expect a few KB/s — fine for status frames (~1 KB), slow for the
  one-time 60 KB anim upload (do that once over USB, or wait it out).
- Suggested test ladder: `GET /api/version` → `POST /display/draw` text →
  anim swap → daemon soak.

The daemon needs no other changes — `make_transport()` is the only
switch point.

---

## 3. What stays core

The renderer's contract (do not grow provider knowledge into it):

- state → ring animation + state word + colors
- `label`/`label_color` → line 1
- `context_pct` → the bar
- `quotas` → line 2 text with worst-quota coloring

If a new provider needs something the schema can't express, extend the
schema (v2) — don't special-case the renderer.
