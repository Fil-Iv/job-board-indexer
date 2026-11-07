#!/bin/sh
# Healthy when the newest backup is less than 26 hours old.
newest=$(find /backups -name 'jobindex-*.dump' -mmin -1560 | head -n 1)
[ -n "$newest" ]
