#!/bin/sh
# Run the normal suite plus scratch-only gateway interoperability.
set -eu
if [ "$#" -ne 4 ]; then
    echo "usage: $0 <gateway-package-directory> <gateway-source-sha> <build> <version>" >&2
    exit 2
fi
export ATC_INTEROP_PACKAGE="$1"
export ATC_INTEROP_GATEWAY_SHA="$2"
export ATC_INTEROP_GATEWAY_BUILD="$3"
export ATC_INTEROP_GATEWAY_VERSION="$4"
exec "$(dirname "$0")/test.sh"
