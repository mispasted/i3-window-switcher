build:
	nix-build -E 'let pkgs = import <nixpkgs> {}; in pkgs.callPackage ./derivation.nix {}'

run: 
	./result/bin/i3-window-switcher -c ./config.json firefox

activate: build run
