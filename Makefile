.PHONY: build run clean test test-trace image load deploy up down status venv calibrate lag traces trace-plot trace-check

IMAGE ?= predictive-autoscaling-app:dev

# Pinned chart versions (reproducibility).
KPS_VERSION  := 92.17.0
KEDA_VERSION := 2.21.0

build:
	cd app/ && go build -o ../bin/pa .

run:
	cd app/ && go run .

clean:
	rm -rf ./bin

test: test-trace
	cd app/ && go test ./...

test-trace:
	python3 -m unittest discover -s loadtest/trace -q

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
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
		--version $(KPS_VERSION) --namespace monitoring --create-namespace \
		-f deploy/monitoring/values.yaml --wait --timeout 10m
	helm upgrade --install keda kedacore/keda \
		--version $(KEDA_VERSION) --namespace keda --create-namespace \
		-f deploy/keda/values.yaml --wait --timeout 5m

# Python tooling for analysis (plots).
venv: .venv/bin/python
.venv/bin/python: experiments/requirements.txt
	python3 -m venv .venv
	.venv/bin/pip install -q -r experiments/requirements.txt
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
traces:
	for s in $(TRACE_SEEDS); do \
		python3 loadtest/trace/generate.py synthetic --seed $$s --out loadtest/trace/out/synthetic-s$$s.json && \
		python3 loadtest/trace/generate.py wikipedia --seed $$s --out loadtest/trace/out/wikipedia-s$$s.json || exit 1; \
	done

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

down:
	kind delete cluster --name pa

status:
	kubectl get nodes -L role -o wide
	kubectl get nodes -o custom-columns='NODE:.metadata.name,CPU_ALLOCATABLE:.status.allocatable.cpu,TAINTS:.spec.taints[*].key'
	kubectl get pods -A -o wide
