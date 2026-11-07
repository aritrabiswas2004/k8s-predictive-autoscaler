.PHONY: build run clean test test-trace test-forecaster test-experiments image load deploy up down status venv calibrate lag traces trace-plot trace-check forecaster-image forecaster-deploy backtest arm arm-clear arm-status arm-check arm-smoke experiment experiment-smoke experiment-plan summarize analyze

IMAGE ?= predictive-autoscaling-app:dev
FORECASTER_IMAGE ?= predictive-autoscaling-forecaster:dev
# Python with the forecaster's dependencies (make venv); CI passes PY=python.
PY ?= .venv/bin/python

# Pinned chart versions (reproducibility).
KPS_VERSION  := 92.17.0
KEDA_VERSION := 2.21.0
METRICS_SERVER_VERSION := 3.14.0

build:
	cd app/ && go build -o ../bin/pa .

run:
	cd app/ && go run .

clean:
	rm -rf ./bin

test: test-trace test-forecaster test-experiments
	cd app/ && go test ./...

test-trace:
	python3 -m unittest discover -s loadtest/trace -q

test-forecaster:
	$(PY) -m pytest forecaster

# Cluster-free checks of the scaling arms (deploy/arms/); make arm-check is the full one.
test-experiments:
	python3 -m unittest discover -s experiments -q

image:
	docker build -t $(IMAGE) app/

# kind nodes can't pull local images, so copy the image into the cluster.
load: image
	kind load docker-image $(IMAGE) --name pa

# The tag stays :dev, so restart to make running pods pick up a freshly loaded image.
deploy: load
	kubectl apply -f deploy/app/
	kubectl rollout restart deployment/web

# Cluster + monitoring + KEDA, from nothing.
up:
	./deploy/kind/up.sh
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm repo add kedacore https://kedacore.github.io/charts --force-update
	helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
		--version $(KPS_VERSION) --namespace monitoring --create-namespace \
		-f deploy/monitoring/values.yaml --wait --timeout 10m
	helm upgrade --install keda kedacore/keda \
		--version $(KEDA_VERSION) --namespace keda --create-namespace \
		-f deploy/keda/values.yaml --wait --timeout 5m
	helm upgrade --install metrics-server metrics-server/metrics-server \
		--version $(METRICS_SERVER_VERSION) --namespace kube-system \
		-f deploy/metrics-server/values.yaml --wait --timeout 5m

# Python tooling: analysis plots and the forecaster (tests, backtest).
venv: .venv/bin/python
.venv/bin/python: experiments/requirements.txt forecaster/requirements.txt forecaster/requirements-dev.txt
	python3 -m venv .venv
	.venv/bin/pip install -q -r experiments/requirements.txt -r forecaster/requirements-dev.txt
	touch .venv/bin/python

# Phase 2: stepped load against one pod; finds the knee and RPS_PER_POD.
CAL_STEPS ?= 40,50,54,57,60,63,66,70
CAL_OUT   ?= experiments/results/calibration/run-$(shell date +%Y%m%d-%H%M%S)
calibrate: venv
	kubectl scale deployment web --replicas=1
	kubectl rollout status deployment/web
	./loadtest/run-k6.sh loadtest/k6/calibrate.js $(CAL_OUT) STEPS=$(CAL_STEPS)
	.venv/bin/python experiments/analysis/calibration.py $(CAL_OUT).json --plot $(CAL_OUT).png

# Phase 2: scale-up lag of one load step (LAG_LOW -> LAG_HIGH RPS) with reactive KEDA.
# RPS_PER_POD comes from docs/methodology.md.
RPS_PER_POD ?= 40
LAG_LOW     ?= 25
LAG_HIGH    ?= 100
LAG_OUT     ?= experiments/results/calibration/lag-$(shell date +%Y%m%d-%H%M%S)
lag:
	./loadtest/measure-lag.sh $(LAG_OUT) $(RPS_PER_POD) $(LAG_LOW) $(LAG_HIGH)

# Phase 3: load traces. Deterministic: the same seed always gives the same file.
TRACE_SEEDS ?= 1 2 3 4 5
# Short trace for smoke tests of the arms: 2 cycles (10 min), no spike. One cycle of history
# is the least that lets arm C forecast anything (seasonal naive repeats the previous cycle).
SMOKE_ARGS := --seed 1 --cycles 2 --history-cycles 1 --spike-rps 0
traces:
	for s in $(TRACE_SEEDS); do \
		python3 loadtest/trace/generate.py synthetic --seed $$s --out loadtest/trace/out/synthetic-s$$s.json && \
		python3 loadtest/trace/generate.py wikipedia --seed $$s --out loadtest/trace/out/wikipedia-s$$s.json || exit 1; \
	done
	python3 loadtest/trace/generate.py synthetic $(SMOKE_ARGS) --out loadtest/trace/out/smoke.json

trace-plot: venv
	.venv/bin/python loadtest/trace/plot.py --out docs/img/traces.png

# Replay one trace against a fixed replica count, no scaler: checks that k6 delivers the
# trace (sent = offered, no dropped iterations) and that the app keeps up at max replicas.
TRACE          ?= synthetic-s1
CHECK_REPLICAS ?= 7
TRACE_OUT      ?= experiments/results/traces/check-$(TRACE)
trace-check:
	@if kubectl get scaledobject,hpa -o name | grep -q .; then echo "remove scalers first" >&2; exit 1; fi
	kubectl scale deployment web --replicas=$(CHECK_REPLICAS)
	kubectl rollout status deployment/web
	K6_FILES=trace.json=loadtest/trace/out/$(TRACE).json ./loadtest/run-k6.sh loadtest/k6/trace.js \
		$(TRACE_OUT) RUN_ID=check-$(TRACE)

# Phase 4: the forecaster. Runs in every arm (shadow mode); only arms C and D scale on it.
forecaster-image:
	docker build -t $(FORECASTER_IMAGE) forecaster/

forecaster-deploy: forecaster-image
	kind load docker-image $(FORECASTER_IMAGE) --name pa
	kubectl apply -f deploy/forecaster/
	kubectl -n monitoring rollout restart deployment/forecaster
	kubectl -n monitoring rollout status deployment/forecaster

# Rolling-origin backtest of the models on every trace and on the recorded runs.
backtest: venv
	$(PY) forecaster/backtest/backtest.py --plot docs/img/backtest.png

# Phase 5: scaling arms (deploy/arms/): a, r100, r80, r60, c, d. One at a time, never two.
ARM ?= r100
arm:
	$(PY) experiments/arms.py apply $(ARM)

arm-clear:
	$(PY) experiments/arms.py clear

arm-status:
	$(PY) experiments/arms.py status

# The arms differ only in signal and threshold (same min/max, behavior, queries).
arm-check:
	$(PY) experiments/arms.py check

# Apply one arm and replay the 10-minute smoke trace against it.
SMOKE_OUT ?= experiments/results/arms/smoke-$(ARM)
arm-smoke: arm
	K6_FILES=trace.json=loadtest/trace/out/smoke.json ./loadtest/run-k6.sh loadtest/k6/trace.js \
		$(SMOKE_OUT) RUN_ID=smoke-$(ARM)
	$(PY) experiments/arms.py status

# Phase 6: the experiment harness (experiments/run_experiment.py). GRID = main | sweep | smoke
# | fault (Phase 8: the forecaster is stopped mid-run).
# Resumable: finished runs are skipped, so re-running the same command continues a grid.
# ONLY / LIMIT select runs, e.g. make experiment GRID=main ONLY=wikipedia LIMIT=12.
# caffeinate keeps macOS from sleeping during a batch (it is skipped where it doesn't exist).
GRID  ?= main
ONLY  ?=
LIMIT ?=
CAFFEINATE := $(shell command -v caffeinate >/dev/null && echo caffeinate -is)
experiment:
	$(CAFFEINATE) $(PY) experiments/run_experiment.py run $(GRID) \
		$(if $(ONLY),--only='$(ONLY)') $(if $(LIMIT),--limit $(LIMIT))

# One run per arm on the 10-minute smoke trace (about 1.5 h): checks the whole pipeline.
experiment-smoke:
	$(CAFFEINATE) $(PY) experiments/run_experiment.py run smoke

experiment-plan:
	$(PY) experiments/run_experiment.py plan $(GRID) $(if $(ONLY),--only='$(ONLY)')

# Recompute every summary row from the saved raw files (no cluster needed).
summarize:
	$(PY) experiments/run_experiment.py summarize $(GRID)

# Every table and figure of the evaluation from the saved runs (no cluster needed):
# docs/results-tables.md, docs/img/{frontier,sweep,timeline-*,forecast-live}.png and
# experiments/results/analysis/runs.csv. Works on a partial grid; every number states its n.
analyze: venv
	$(PY) experiments/analysis/analyze.py

down:
	kind delete cluster --name pa

status:
	kubectl get nodes -L role -o wide
	kubectl get nodes -o custom-columns='NODE:.metadata.name,CPU_ALLOCATABLE:.status.allocatable.cpu,TAINTS:.spec.taints[*].key'
	kubectl get pods -A -o wide
