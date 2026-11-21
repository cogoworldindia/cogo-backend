// Spec §13 M8: "All DLQs are empty after a 1-hour soak". Every flow at once, at a steady,
// moderate rate, for DURATION (default 1h); then `make dlq` checks the dead-letter queues.
//   make load-soak            (DURATION=1h; FEED_RATE=30/s, JOURNEY_RATE=60/min,
//                              SOCKETS=300, RATE=10 msg/s)
// The flows are the other scripts' own functions, so the soak tests what they test.
import scroll from "./feed.js";
import create from "./journeys.js";
import { listen, send, mustReceive } from "./sockets.js";

export { listen, send, scroll, create };

// make load-soak exports DURATION (default 1h): listen() reads it too, for its hold time.
const DURATION = __ENV.DURATION || "1h";
const SOCKETS = Number(__ENV.SOCKETS || 300);
const RATE = Number(__ENV.RATE || 10);

export const options = {
  scenarios: {
    scroll: {
      executor: "constant-arrival-rate", exec: "scroll", rate: Number(__ENV.FEED_RATE || 30),
      timeUnit: "1s", duration: DURATION, preAllocatedVUs: 30, maxVUs: 200,
    },
    create: {
      executor: "constant-arrival-rate", exec: "create", rate: Number(__ENV.JOURNEY_RATE || 60),
      timeUnit: "1m", duration: DURATION, preAllocatedVUs: 10, maxVUs: 100,
    },
    listeners: {
      executor: "per-vu-iterations", exec: "listen", vus: SOCKETS,
      iterations: 1, maxDuration: "2h",
    },
    senders: {
      executor: "constant-arrival-rate", exec: "send", rate: RATE,
      timeUnit: "1s", duration: DURATION, startTime: "30s", preAllocatedVUs: 10, maxVUs: 100,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{page:first}": ["p(95)<300"],
    banner_filled: ["p(95)<10000"],
    message_delivery: ["p(95)<500"],
    socket_errors: ["count<3"],
    messages_received: mustReceive(SOCKETS, RATE, DURATION),
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};
