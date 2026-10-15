.DEFAULT_GOAL := run
MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
PYTHON = ./venv/bin/python3
STAMP = ./venv/.venv-setup-complete
 
# Default
run: $(STAMP)
	$(PYTHON) i3_window_switcher.py -c ./config.json browser

# Check nix build 
nix-build:
	nix-build -E 'let pkgs = import <nixpkgs> {}; in pkgs.callPackage ./derivation.nix {}'

$(STAMP): 
	python3 -m venv ./venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install i3ipc
	touch $(STAMP)
# This creates the venv by looking for the "stamp" file
venv: $(STAMP)


	