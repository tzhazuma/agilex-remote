# Shared connection defaults for the AgileX helpers.
#
# This file contains NO credentials. The sudo password is never stored here:
# scripts/rsudo.sh reads AGILEX_SUDO_PASSWORD from the environment or prompts.
# The web UI password lives on the robot in /etc/agilex-remote.env (0600).
#
# AGILEX_HOST is the robot's mDNS name, NOT an IP: the robot is on DHCP, so its
# address changes on every reboot. avahi-daemon on the robot keeps
# "agilex.local" pointed at the current address. If mDNS fails, run
# scripts/find-robot.sh or set AGILEX_HOST explicitly.
AGILEX_HOST="${AGILEX_HOST:-agilex.local}"
AGILEX_USER="${AGILEX_USER:-agilex}"
AGILEX_SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=8)
