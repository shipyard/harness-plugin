IMAGE ?= ghcr.io/shipyard/harness-plugin
TAG ?= dev

.PHONY: test lint docker.build

test:
	python -m pytest -q

lint:
	ruff check . && ruff format --check .

docker.build:
	docker build -t $(IMAGE):$(TAG) .
