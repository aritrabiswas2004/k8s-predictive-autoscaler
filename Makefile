.PHONY: build run

build:
	go build -o bin/pa .

run:
	go run .

clean:
	rm -rf ./bin

test:
	go test
