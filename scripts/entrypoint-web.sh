#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Create or align user and group
if ! getent group uav >/dev/null; then
    groupadd -g "$PGID" uav 2>/dev/null || true
fi

if ! id -u uav >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -m -s /bin/sh uav 2>/dev/null || true
fi

# Ensure directories exist and have proper ownership
mkdir -p /downloads /config
chown -R "$PUID:$PGID" /downloads /config 2>/dev/null || true

# Execute command as uav user or root
if [ "$PUID" -ne 0 ]; then
    exec gosu "$PUID:$PGID" "$@"
else
    exec "$@"
fi
