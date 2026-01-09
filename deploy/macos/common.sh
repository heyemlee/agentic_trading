#!/bin/zsh
# Resolve the two directories every deployment script needs, and nothing else.
#
#   release_dir  the code and its .venv, pinned for the life of a release
#   data_dir     config/, state/ and runs/, which outlive every release
#
# In a single-checkout install both are the checkout, which is the behaviour these scripts had
# before release.sh existed. launchd exports CT_RELEASE_DIR and CT_DATA_DIR once a release
# layout is in use.

# launchd exports both when it starts a job. Run by hand there is no such environment, and a
# script that then fell back to its own checkout would check the wrong thing entirely: after a
# release install, "is the service healthy" is a question about the release, not about whatever
# the developer is editing. The installed job is the record of what is actually deployed, so
# that is where the layout is read from.
installed_plist="$HOME/Library/LaunchAgents/com.local.agentic-trading.api.plist"
if [[ -r "$installed_plist" ]]; then
  : ${CT_RELEASE_DIR:=$(plutil -extract EnvironmentVariables.CT_RELEASE_DIR raw -o - \
      "$installed_plist" 2>/dev/null || true)}
  : ${CT_DATA_DIR:=$(plutil -extract EnvironmentVariables.CT_DATA_DIR raw -o - \
      "$installed_plist" 2>/dev/null || true)}
fi

release_dir="${CT_RELEASE_DIR:-$(cd "$(dirname "$0")/../.." && pwd -P)}"
data_dir="${CT_DATA_DIR:-$release_dir}"

if [[ ! -x "$release_dir/.venv/bin/python" ]]; then
  echo "no virtual environment at $release_dir/.venv; run 'uv sync' in that release" >&2
  exit 1
fi
if [[ ! -d "$data_dir/config" ]]; then
  echo "no config directory at $data_dir/config" >&2
  exit 1
fi

cd "$data_dir"
