# Usage (from the repository root or this directory):
#   make -C docker context EXTRA=/path/to/extra_dir
#   make -C docker image TAG=20261116c
# DOCKER may be set to e.g. "sudo docker -H unix:///run/docker.sock".
PYTHON ?= python3
DOCKER ?= docker
TAG    ?= dev
EXTRA  ?=
BUILD  := build
HERE   := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

.PHONY: context image deps clean

context:
	$(PYTHON) $(HERE)tools/make_context.py --out $(HERE)$(BUILD) $(if $(EXTRA),--extra $(EXTRA),)
	$(PYTHON) $(HERE)tools/make_manifest.py --out $(HERE)$(BUILD)

deps:
	$(DOCKER) build --platform linux/amd64 --target deps -t datamate-runtime-deps:$(TAG) \
	  -f $(HERE)$(BUILD)/context/Dockerfile $(HERE)$(BUILD)/context

image: context
	sed 's#datamate-runtime-deps:[0-9A-Za-z._-]*#datamate-runtime-deps:$(TAG)#' $(HERE)$(BUILD)/context/Dockerfile.build > $(HERE)$(BUILD)/context/Dockerfile.run
	$(DOCKER) build --platform linux/amd64 -t datamate-runtime:$(TAG) \
	  -f $(HERE)$(BUILD)/context/Dockerfile.run $(HERE)$(BUILD)/context

clean:
	rm -rf $(HERE)$(BUILD)
