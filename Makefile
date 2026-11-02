.PHONY: build run clean test image deploy

IMAGE ?= predictive-autoscaling-app:dev

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

deploy:
	kubectl apply -f k8s/
