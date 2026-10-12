#!/usr/bin/env bash
# Stops what ./start-all.sh started.
cd "$(dirname "${BASH_SOURCE[0]}")"
for pidfile in .run/*.pid; do
  [[ -f "$pidfile" ]] || continue
  name="$(basename "$pidfile" .pid)"
  if kill "$(cat "$pidfile")" 2>/dev/null; then echo "$name: stopped"; else echo "$name: was not running"; fi
  rm -f "$pidfile"
done
