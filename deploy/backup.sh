#!/bin/sh
# Consistent SQLite backup, then prune copies older than 60 days.
set -eu

cd /opt/checklist

stamp="$(date +%F)"
.venv/bin/python main.py backup --out "/var/lib/checklist/backup-${stamp}.db"

find /var/lib/checklist -maxdepth 1 -name 'backup-*.db' -mtime +60 -delete
