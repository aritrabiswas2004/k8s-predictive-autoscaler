// Phase 2 scale-up lag: hold LOW_RPS for LOW_SECONDS, then jump to HIGH_RPS for HIGH_SECONDS.
// The first request of the high phase logs STEP_AT=<unix ms>, the t0 of the lag breakdown.

import http from 'k6/http';
import exec from 'k6/execution';

const TARGET = __ENV.TARGET || 'http://web.default.svc.cluster.local/work';
const LOW_RPS = Number(__ENV.LOW_RPS || 20);
const HIGH_RPS = Number(__ENV.HIGH_RPS || 100);
const LOW_SECONDS = Number(__ENV.LOW_SECONDS || 90);
const HIGH_SECONDS = Number(__ENV.HIGH_SECONDS || 150);

export const options = {
  scenarios: {
    low: {
      executor: 'constant-arrival-rate', rate: LOW_RPS, timeUnit: '1s',
      duration: `${LOW_SECONDS}s`, preAllocatedVUs: 50, maxVUs: 2000,
    },
    high: {
      executor: 'constant-arrival-rate', rate: HIGH_RPS, timeUnit: '1s',
      duration: `${HIGH_SECONDS}s`, startTime: `${LOW_SECONDS}s`,
      preAllocatedVUs: 200, maxVUs: 2000,
    },
  },
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max'],
  discardResponseBodies: true,
};

export default function () {
  if (exec.scenario.name === 'high' && exec.scenario.iterationInTest === 0) {
    console.log(`STEP_AT=${Date.now()}`);
  }
  http.get(TARGET, { timeout: '10s' });
}

export function handleSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const out = {
    config: { TARGET, LOW_RPS, HIGH_RPS, LOW_SECONDS, HIGH_SECONDS },
    requests: data.metrics.http_reqs.values.count,
    dropped: data.metrics.dropped_iterations ? data.metrics.dropped_iterations.values.count : 0,
    failed_rate: data.metrics.http_req_failed.values.rate,
    p50_ms: d.med, p95_ms: d['p(95)'], p99_ms: d['p(99)'], max_ms: d.max,
  };
  return { stdout: `\n===SUMMARY-BEGIN===\n${JSON.stringify(out)}\n===SUMMARY-END===\n` };
}
