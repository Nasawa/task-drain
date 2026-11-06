#!/bin/bash
# exit 0 => the world is ready for the action
test -d "${TMPDIR:-/tmp}"
