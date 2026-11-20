#!/bin/bash
# Double-click in Finder: builds DEMASCAS.app, installs it in /Applications, starts it.
cd "$(dirname "$0")" || exit 1
./scripts/build_app.sh
echo ""
read -n 1 -s -r -p "Press any key to close this window."
