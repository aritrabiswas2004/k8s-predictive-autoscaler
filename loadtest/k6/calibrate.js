// Phase 2 capacity calibration: a stepped open-model load against a fixed number of pods.
//
// Each step is its own constant-arrival-rate scenario, so k6 keeps sending at the requested
// rate however slow the responses get (a closed model would slow down with the server and
// hide the knee). Steps are separated by an idle gap so a backlog from an overloaded step can
// drain before the next one starts. The first WARM_SECONDS of every step are tagged m=n and
// excluded from the per-step numbers.
//
// Env: TARGET, STEPS (comma-separated RPS), STEP_SECONDS, WARM_SECONDS, GAP_SECONDS.

import http from 'k6/http';
import exec from 'k6/execution';

const TARGET = __ENV.TARGET || 'http://web.default.svc.cluster.local/work';
const STEPS = (__ENV.STEPS || '10,20,30,40,50,60,70,80').split(',').map(Number);
const STEP_SECONDS = Number(__ENV.STEP_SECONDS || 50);
const WARM_SECONDS = Number(__ENV.WARM_SECONDS || 10);
const GAP_SECONDS = Number(__ENV.GAP_SECONDS || 10);

const scenarios = {};
const thresholds = {};
STEPS.forEach((rate, i) => {
  scenarios[`s${rate}`] = {
    executor: 'constant-arrival-rate',
    rate,
    timeUnit: '1s',
    duration: `${STEP_SECONDS}s`,
    startTime: `${i * (STEP_SECONDS + GAP_SECONDS)}s`,
    // VUs needed = rate x latency; generous so k6 never drops iterations because of us.
    preAllocatedVUs: Math.max(20, rate),
    maxVUs: 2000,
    env: { RATE: String(rate) },
  };
  // Thresholds only exist to make k6 report these sub-metrics in the summary, so they
  // always pass (a failing one would make k6 exit 99 for a step that is meant to overload).
  const sel = `step:${rate},m:y`;
  thresholds[`http_req_duration{${sel}}`] = ['p(95)>=0'];
  thresholds[`http_req_failed{${sel}}`] = ['rate>=0'];
  thresholds[`http_reqs{${sel}}`] = ['count>=0'];
  thresholds[`dropped_iterations{scenario:s${rate}}`] = ['count>=0'];
});

export const options = {
  scenarios,
  thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
  discardResponseBodies: true,
};

export default function () {
  const elapsed = (Date.now() - exec.scenario.startTime) / 1000;
  http.get(TARGET, {
    timeout: '10s',
    tags: { step: __ENV.RATE, m: elapsed >= WARM_SECONDS ? 'y' : 'n' },
  });
}

// Printed between markers so run-k6.sh can cut it out of the pod log.
export function handleSummary(data) {
  const rows = STEPS.map((rate) => {
    const sel = `step:${rate},m:y`;
    const d = data.metrics[`http_req_duration{${sel}}`];
    const f = data.metrics[`http_req_failed{${sel}}`];
    const n = data.metrics[`http_reqs{${sel}}`];
    const drop = data.metrics[`dropped_iterations{scenario:s${rate}}`];
    return {
      target_rps: rate,
      // count over the measured part of the step (http_reqs' own rate is over the whole test)
      achieved_rps: n ? n.values.count / (STEP_SECONDS - WARM_SECONDS) : 0,
      requests: n ? n.values.count : 0,
      dropped: drop ? drop.values.count : 0,
      failed_rate: f ? f.values.rate : null,
      p50_ms: d ? d.values.med : null,
      p95_ms: d ? d.values['p(95)'] : null,
      p99_ms: d ? d.values['p(99)'] : null,
      max_ms: d ? d.values.max : null,
    };
  });
  const out = { config: { TARGET, STEPS, STEP_SECONDS, WARM_SECONDS, GAP_SECONDS }, steps: rows };
  return { stdout: `\n===SUMMARY-BEGIN===\n${JSON.stringify(out)}\n===SUMMARY-END===\n` };
}
