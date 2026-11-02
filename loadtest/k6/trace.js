// Replay a load trace from loadtest/trace/generate.py as an open-model arrival rate.
//
// One ramping-arrival-rate scenario with a 5 s linear stage per trace point. The trace is
// mounted next to this script by run-k6.sh (K6_FILES=trace.json=<path>).
//
// No connection reuse: every request opens a new TCP connection. kube-proxy balances
// connections, not requests, so with keep-alive the load stays pinned to the pods that
// existed when the connections were opened and a scale-up barely helps (measured in Phase 2,
// docs/methodology.md). New connections spread every request across the ready pods, like
// traffic from many independent clients. The same setting is used for every arm.
//
// Every request is tagged with its cycle, part (history or scored) and whether it falls in
// the spike, so the summary and the remote-written metrics can be split by them. Global tags
// workload/run label all k6 series in Prometheus.
//
// Env: TARGET, TRACE (default /scripts/trace.json), RUN_ID, MAX_VUS.

import http from 'k6/http';
import exec from 'k6/execution';

const TARGET = __ENV.TARGET || 'http://web.default.svc.cluster.local/work';
const trace = JSON.parse(open(__ENV.TRACE || '/scripts/trace.json'));
const STEP = trace.step_seconds;
const CYCLE = trace.cycle_seconds;
const MAX_VUS = Number(__ENV.MAX_VUS || 3000);

// k6 takes integer rates per timeUnit. The trace has one decimal, so count per 10 s.
const PER = 10;
const stages = trace.rps.slice(1).map((r) => ({ duration: `${STEP}s`, target: Math.round(r * PER) }));

const thresholds = {};
// Always-passing thresholds: they only make k6 report these sub-metrics in the summary.
const groups = [];
for (let c = 0; c < trace.num_cycles; c++) groups.push(`cycle:${c}`);
groups.push('part:history', 'part:scored', 'spike:y');
for (const g of groups) {
  thresholds[`http_req_duration{${g}}`] = ['p(95)>=0'];
  thresholds[`http_req_failed{${g}}`] = ['rate>=0'];
  thresholds[`http_reqs{${g}}`] = ['count>=0'];
}
thresholds.dropped_iterations = ['count>=0'];

export const options = {
  scenarios: {
    trace: {
      executor: 'ramping-arrival-rate',
      startRate: Math.round(trace.rps[0] * PER),
      timeUnit: `${PER}s`,
      stages,
      // VUs needed = rate x latency. Healthy that is a handful; under overload requests wait
      // up to the 10 s timeout, so a 250 RPS spike can need ~2500. Running out of VUs would
      // drop iterations and hide load, so maxVUs is generous and dropped_iterations reported.
      // Starting a VU mid-run takes long enough to drop a few iterations (the first
      // Wikipedia check needed 102 of 100 pre-allocated and dropped 7), so pre-allocate 200.
      preAllocatedVUs: 200,
      maxVUs: MAX_VUS,
    },
  },
  noVUConnectionReuse: true,
  discardResponseBodies: true,
  thresholds,
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max'],
  tags: { workload: trace.workload, trace: trace.name, run: __ENV.RUN_ID || 'adhoc' },
};

export default function () {
  if (exec.scenario.iterationInTest === 0) console.log(`TRACE_START=${exec.scenario.startTime}`);
  const t = (Date.now() - exec.scenario.startTime) / 1000;
  const cycle = Math.min(Math.floor(t / CYCLE), trace.num_cycles - 1);
  const inSpike = trace.spike && t >= trace.spike.start_s && t <= trace.spike.end_s;
  http.get(TARGET, {
    timeout: '10s',
    tags: {
      cycle: String(cycle),
      part: cycle < trace.history_cycles ? 'history' : 'scored',
      spike: inSpike ? 'y' : 'n',
    },
  });
}

// Requests the trace asks for between two times (trapezoids over the points).
function offered(from, to) {
  let n = 0;
  for (let i = 0; i + 1 < trace.rps.length; i++) {
    const a = Math.max(from, i * STEP);
    const b = Math.min(to, (i + 1) * STEP);
    if (b <= a) continue;
    const at = (x) => trace.rps[i] + ((trace.rps[i + 1] - trace.rps[i]) * (x - i * STEP)) / STEP;
    n += ((at(a) + at(b)) / 2) * (b - a);
  }
  return n;
}

function group(data, g) {
  const d = data.metrics[`http_req_duration{${g}}`];
  const f = data.metrics[`http_req_failed{${g}}`];
  const n = data.metrics[`http_reqs{${g}}`];
  return {
    requests: n ? n.values.count : 0,
    failed_rate: f ? f.values.rate : null,
    p50_ms: d ? d.values.med : null,
    p95_ms: d ? d.values['p(95)'] : null,
    p99_ms: d ? d.values['p(99)'] : null,
    max_ms: d ? d.values.max : null,
  };
}

export function handleSummary(data) {
  const cycles = [];
  for (let c = 0; c < trace.num_cycles; c++) {
    const g = group(data, `cycle:${c}`);
    g.cycle = c;
    g.offered = Math.round(offered(c * CYCLE, (c + 1) * CYCLE));
    g.sent_ratio = g.offered ? g.requests / g.offered : null;
    cycles.push(g);
  }
  const d = data.metrics.http_req_duration.values;
  const vus = data.metrics.vus_max ? data.metrics.vus_max.values.max : null;
  const out = {
    config: { TARGET, trace: trace.name, workload: trace.workload, seed: trace.seed,
              run: __ENV.RUN_ID || 'adhoc', max_vus: MAX_VUS, no_connection_reuse: true },
    offered: Math.round(offered(0, trace.duration_seconds)),
    requests: data.metrics.http_reqs.values.count,
    dropped: data.metrics.dropped_iterations ? data.metrics.dropped_iterations.values.count : 0,
    vus_max: vus,
    failed_rate: data.metrics.http_req_failed.values.rate,
    p50_ms: d.med, p95_ms: d['p(95)'], p99_ms: d['p(99)'], max_ms: d.max,
    history: group(data, 'part:history'),
    scored: group(data, 'part:scored'),
    spike: group(data, 'spike:y'),
    cycles,
  };
  return { stdout: `\n===SUMMARY-BEGIN===\n${JSON.stringify(out)}\n===SUMMARY-END===\n` };
}
