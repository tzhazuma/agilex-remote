# Shared connection config for AgileX robot helpers.
# Keep this file chmod 600 — it holds the sudo password.
#
# AGILEX_HOST is the robot's mDNS name, NOT an IP: the robot is on eduroam
# Wi-Fi with DHCP, so its address changes on every reboot (it has been
# <robot-ip> and then <robot-ip>). avahi-daemon on the robot keeps
# "agilex.local" pointed at the current address, and that name resolves fine
# across the campus VPN. If mDNS ever fails, run bin/find-robot.sh.
AGILEX_HOST=agilex.local
AGILEX_USER=agilex
AGILEX_PW=agx
AGILEX_SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=8)
