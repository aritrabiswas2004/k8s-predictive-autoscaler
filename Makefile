.PHONY: build run clean test image load deploy up down status

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

test:
	cd app/ && go test ./...

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

down:
	kind delete cluster --name pa

status:
	kubectl get nodes -L role -o wide
	kubectl get nodes -o custom-columns='NODE:.metadata.name,CPU_ALLOCATABLE:.status.allocatable.cpu,TAINTS:.spec.taints[*].key'
	kubectl get pods -A -o wide
