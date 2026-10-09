#!/bin/bash
set -euo pipefail

VERSION=$(cat VERSION.txt)
GIT_COMMIT=$(git -c core.fsmonitor=false rev-parse --verify HEAD)
echo "STABLE_VERSION ${VERSION}"
echo "STABLE_GIT_COMMIT ${GIT_COMMIT}"
