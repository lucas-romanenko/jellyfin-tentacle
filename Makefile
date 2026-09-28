# check: the full unit suite (what the workbench's pre-push hook runs).
#
# deploy, verify, deploy-release: run the current main on the maintainer's own
# server without a release, check it, and switch back to the latest public
# release. They call a private script that knows the server (nothing about it
# lives in this public repo); nothing is pushed to a registry or a release.
# Point TENTACLE_DEPLOY at your own script to use them elsewhere.
TENTACLE_DEPLOY ?= $(HOME)/homelab/scripts/tentacle-deploy

.PHONY: check deploy verify deploy-release
check:
	scripts/check

deploy verify deploy-release:
	@test -x "$(TENTACLE_DEPLOY)" || { echo "no deploy script at $(TENTACLE_DEPLOY) (set TENTACLE_DEPLOY)"; exit 1; }
	@TENTACLE_REPO="$(CURDIR)" "$(TENTACLE_DEPLOY)" $@
