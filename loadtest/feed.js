// Spec §12: "Feed scroll at 200 rps". §11: feed p95 < 300 ms.
//   make load-feed            (RATE=200 DURATION=2m by default)
import http from "k6/http";
import { SharedArray } from "k6/data";
import { check } from "k6";

// One copy shared by every VU (see sockets.js)
const users = new SharedArray("users", () => JSON.parse(open("./.seed.json")).users);
const BASE = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
  scenarios: {
    feed: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 200),
      timeUnit: "1s",
      duration: __ENV.DURATION || "2m",
      preAllocatedVUs: 100,
      maxVUs: 600,
    },
  },
  thresholds: {
    "http_req_duration{page:first}": ["p(95)<300"],
    "http_req_duration{page:next}": ["p(95)<300"],
    http_req_failed: ["rate<0.01"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export default function () {
  const user = users[Math.floor(Math.random() * users.length)];
  const headers = { Authorization: `Bearer ${user.token}` };
  const first = http.get(`${BASE}/v1/feed`, { headers, tags: { page: "first", name: "GET /v1/feed" } });
  check(first, { "feed 200": (r) => r.status === 200 });
  const next = first.status === 200 ? first.json("data.next_cursor") : null;
  if (next && Math.random() < 0.3) {  // a third of users scroll on
    const second = http.get(`${BASE}/v1/feed?cursor=${encodeURIComponent(next)}`, { headers, tags: { page: "next", name: "GET /v1/feed?cursor" } });
    check(second, { "page 2 200": (r) => r.status === 200 });
  }
}
