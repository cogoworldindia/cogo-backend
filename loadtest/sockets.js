// Spec §12: "10k concurrent sockets with a 50 msg/s chat mix". §11: message delivery p95 <
// 500 ms. Listeners hold sockets for the seed users (several per user, like several devices);
// senders post into the seeded conversations at 50 msg/s; each listener times the message.new
// frame against the send time in the body.
//   make load-ws              (SOCKETS=1000 RATE=50 DURATION=2m)
import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Counter } from "k6/metrics";
import { WebSocket } from "k6/experimental/websockets";  // k6 1.3: not graduated yet
import { SharedArray } from "k6/data";

// SharedArray: one copy for all VUs. A plain parse per VU took the host down at 10k sockets.
const users = new SharedArray("users", () => JSON.parse(open("./.seed.json")).users);
const pairs = new SharedArray("pairs", () => JSON.parse(open("./.seed.json")).pairs);
const BASE = __ENV.BASE_URL || "http://localhost:8000";
const WS = BASE.replace("http", "ws") + "/v1/ws?token=";
const SOCKETS = Number(__ENV.SOCKETS || 1000);
// Listeners hold their sockets for 30 s + DURATION + 10 s, so a script that reuses listen()
// (soak.js) must pass the same DURATION in the environment (make load-soak does).
const DURATION = __ENV.DURATION || "2m";
const RATE = Number(__ENV.RATE || 50);
const delivery = new Trend("message_delivery", true);
const received = new Counter("messages_received");
const opened = new Counter("sockets_opened");
const failed = new Counter("socket_errors");

export const options = {
  scenarios: {
    listeners: { executor: "per-vu-iterations", vus: SOCKETS, iterations: 1, maxDuration: "10m", exec: "listen" },
    senders: {
      executor: "constant-arrival-rate", exec: "send", rate: RATE, timeUnit: "1s",
      duration: DURATION, startTime: "30s", preAllocatedVUs: 50, maxVUs: 200,
    },
  },
  thresholds: {
    message_delivery: ["p(95)<500"],
    "checks{scenario:senders}": ["rate>0.99"],
    socket_errors: [`count<${Math.ceil(SOCKETS / 100)}`],  // under 1% of the sockets
    messages_received: mustReceive(SOCKETS, RATE, DURATION),
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export function seconds(d) { const m = /^(\d+)([smh])$/.exec(d); return Number(m[1]) * { s: 1, m: 60, h: 3600 }[m[2]]; }

// When every user holds a socket, every message has a listener: at least 95% of what the
// senders post must arrive (a listener that left early, or a lost subscription, fails this).
export function mustReceive(sockets, rate, duration) {
  return sockets >= users.length ? [`count>${Math.floor(rate * seconds(duration) * 0.95)}`] : [];
}

export function listen() {
  const user = users[(__VU - 1) % users.length];
  sleep(Math.random() * 10);  // connects spread over 10 s, as jittered clients would
  const ws = new WebSocket(WS + user.token);
  const holdMs = (30 + seconds(DURATION) + 10) * 1000;
  const t0 = Date.now();
  let closing = false;
  ws.onopen = () => {
    opened.add(1);
    const ping = setInterval(() => ws.send(JSON.stringify({ type: "ping" })), 25000);
    ws.onclose = () => clearInterval(ping);  // e.g. refused with 1013: stop pinging a dead socket
    setTimeout(() => { closing = true; clearInterval(ping); ws.close(); }, holdMs);
  };
  ws.onerror = (e) => {
    // k6 reports a pong it owes the server after its own close frame as "close sent":
    // a client teardown race, not a failure
    if (closing) return;
    failed.add(1);
    console.warn(`socket ${__VU} after ${Math.round((Date.now() - t0) / 1000)}s: ${e.error}`);
  };
  ws.onmessage = (event) => {
    const frame = JSON.parse(event.data);
    if (frame.type === "message.new" && frame.data && /^t:\d+$/.test(frame.data.body || "") && frame.data.sender_id !== user.id) {
      delivery.add(Date.now() - Number(frame.data.body.slice(2)));
      received.add(1);
    }
  };
}

export function send() {
  const pair = pairs[Math.floor(Math.random() * pairs.length)];
  const fromA = Math.random() < 0.5;
  const r = http.post(
    `${BASE}/v1/conversations/${pair.conversation}/messages`,
    JSON.stringify({ client_msg_id: `${__VU}-${__ITER}-${Date.now()}`, body: `t:${Date.now()}` }),
    // one metric name for every conversation: per-URL tags made k6 track 200k time series
    { headers: { Authorization: `Bearer ${fromA ? pair.a_token : pair.b_token}`, "Content-Type": "application/json" },
      tags: { name: "POST /v1/conversations/{id}/messages" } },
  );
  check(r, { "sent 201": (x) => x.status === 201 || x.status === 200 });
}
