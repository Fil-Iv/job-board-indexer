#!/bin/sh
# Nightly PostgreSQL backups (runs in the `backup` service of docker-compose.prod.yml).
#
# Every day at BACKUP_HOUR (UTC) writes /backups/jobindex-YYYYMMDD-HHMMSS.dump
# (pg_dump custom format, compressed) and deletes dumps older than BACKUP_KEEP_DAYS.
# `backup.sh now` makes one backup immediately and exits.
# Restore: see docs/DEPLOY.md ("Restore a backup").
set -eu

: "${BACKUP_HOUR:=3}"
: "${BACKUP_KEEP_DAYS:=14}"
export PGHOST=db PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" PGDATABASE="$POSTGRES_DB"

backup() {
	file="/backups/jobindex-$(date -u +%Y%m%d-%H%M%S).dump"
	echo "backup: writing $file"
	pg_dump --format=custom --compress=6 --file="$file.partial"
	mv "$file.partial" "$file"
	find /backups -name 'jobindex-*.dump' -mtime +"$BACKUP_KEEP_DAYS" -print -delete
	echo "backup: done ($(du -h "$file" | cut -f1))"
}

if [ "${1:-}" = "now" ]; then
	backup
	exit 0
fi

# First backup right away if none exists yet, then daily.
ls /backups/jobindex-*.dump >/dev/null 2>&1 || backup
while true; do
	now=$(date -u +%s)
	next=$(date -u -d "$(date -u +%Y-%m-%d) ${BACKUP_HOUR}:00" +%s)
	[ "$next" -le "$now" ] && next=$((next + 86400))
	sleep $((next - now))
	backup || echo "backup: FAILED" >&2
done
