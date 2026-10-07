#!/bin/sh
# Run e2e/run.py on a docker host over ssh: ships this tree's tracked and new files (not .gitignored ones),
# runs there, and brings the results back to ./e2e-out/<run>.
# usage: e2e/remote.sh USER@HOST [run.py arguments...]     e.g. e2e/remote.sh me@dockerhost linux
# NANOTEA_CI_SSH: extra ssh options, e.g. "-o IdentityAgent=$SSH_AUTH_SOCK".
set -eu
[ $# -ge 2 ] || { echo "usage: e2e/remote.sh USER@HOST SCENARIO... (see python3 e2e/run.py --help)" >&2; exit 2; }
host=$1
shift
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
run=$(date +%Y%m%d-%H%M%S)
remote=/tmp/nanotea-ci-src-$run
ssh_() { ssh -o BatchMode=yes ${NANOTEA_CI_SSH:-} "$host" "$@"; }

cd "$root"
git ls-files -z --cached --others --exclude-standard | tar --null -T - -czf - | ssh_ "mkdir -p $remote && tar -xzf - -C $remote"
rc=0
ssh_ "cd $remote && python3 e2e/run.py --out /tmp/nanotea-ci-$run $*" || rc=$?
mkdir -p "e2e-out/$run"
ssh_ "tar -C /tmp/nanotea-ci-$run -czf - ." | tar -xzf - -C "e2e-out/$run"
ssh_ "rm -rf $remote /tmp/nanotea-ci-$run"
echo "results: $root/e2e-out/$run"
exit $rc
