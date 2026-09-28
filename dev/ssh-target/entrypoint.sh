#!/bin/sh
set -eu
# Fixed host key from .dev (scripts/dev-keys.sh), so the known_hosts line in OpenBao is
# predictable. Only offer that key.
install -m 0600 /host_key /etc/ssh/ssh_host_ed25519_key
install -m 0644 /host_key.pub /etc/ssh/ssh_host_ed25519_key.pub
install -d -o ansible -g ansible -m 0700 /home/ansible/.ssh
install -o ansible -g ansible -m 0600 /authorized_keys /home/ansible/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e -o HostKey=/etc/ssh/ssh_host_ed25519_key
