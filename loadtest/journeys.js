// Spec §12: "500 journeys/min creation with matching". §11: POST /journeys -> match banner
// filled, p95 < 10 s at 500 new journeys/min. Each iteration posts a journey, polls it until
// matching is done, then deletes it (so users stay under the 5-upcoming limit).
//   make load-journeys        (RATE=500 per minute, DURATION=3m)
import http from "k6/http";
import { SharedArray } from "k6/data";
import { check, sleep } from "k6";
import { Trend, Counter } from "k6/metrics";

// One copy shared by every VU (see sockets.js)
const users = new SharedArray("users", () => JSON.parse(open("./.seed.json")).users);
const BASE = __ENV.BASE_URL || "http://localhost:8000";
const ORIGINS = ["mock:noida-uttar-pradesh", "mock:delhi-delhi", "mock:gurugram-haryana", "mock:ghaziabad-uttar-pradesh"];
const DESTS = ["mock:rishikesh-uttarakhand", "mock:manali-himachal-pradesh", "mock:jaipur-rajasthan", "mock:panaji-goa"];
const banner = new Trend("banner_filled", true);
const notDone = new Counter("banner_not_filled");
const skipped = new Counter("journey_limit_skipped");

export const options = {
  scenarios: {
    journeys: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 500),
      timeUnit: "1m",
      duration: __ENV.DURATION || "3m",
      preAllocatedVUs: 50,
      maxVUs: 300,
    },
  },
  thresholds: {
    banner_filled: ["p(95)<10000"],
    "http_req_duration{name:create}": ["p(95)<500"],
    banner_not_filled: ["count<5"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const pick = (xs) => xs[Math.floor(Math.random() * xs.length)];

export default function () {
  const user = users[(__VU * 7919 + __ITER) % users.length];
  const headers = { Authorization: `Bearer ${user.token}`, "Content-Type": "application/json" };
  const start = new Date(Date.now() + (2 + Math.random() * 40) * 86400000);
  start.setUTCMinutes(0, 0, 0);
  const body = JSON.stringify({
    origin_place_id: pick(ORIGINS),
    destination_place_id: pick(DESTS),
    start_at: start.toISOString(),
    end_at: new Date(start.getTime() + 54 * 3600000).toISOString(),
    who_can_join: "anyone",
    group_size: 4,
  });
  const t0 = Date.now();  // the banner clock starts at the POST (spec §11)
  const created = http.post(`${BASE}/v1/journeys`, body, { headers, tags: { name: "create" } });
  if (created.status === 409) { skipped.add(1); return; }  // that user is at 5 upcoming
  if (!check(created, { "created 201": (r) => r.status === 201 })) return;
  const id = created.json("data.id");
  let done = false;
  while (Date.now() - t0 < 20000) {
    const r = http.get(`${BASE}/v1/journeys/${id}`, { headers, tags: { name: "poll" } });
    if (r.status === 200 && r.json("data.matches.state") === "done") { done = true; break; }
    sleep(0.25);
  }
  if (done) banner.add(Date.now() - t0); else notDone.add(1);
  http.del(`${BASE}/v1/journeys/${id}`, null, { headers, tags: { name: "delete" } });
}
