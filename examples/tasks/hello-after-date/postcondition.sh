#!/bin/bash
# exit 0 => PROVES the action took effect
grep -qx "hello from task-drain" "${TMPDIR:-/tmp}/task-drain-hello.txt"
