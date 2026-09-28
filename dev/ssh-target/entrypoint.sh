#!/bin/sh
set -eu
# Vaste host key uit .dev (scripts/dev-keys.sh), zodat de known_hosts-regel in OpenBao
# voorspelbaar is. Alleen die key aanbieden.
install -m 0600 /host_key /etc/ssh/ssh_host_ed25519_key
install -m 0644 /host_key.pub /etc/ssh/ssh_host_ed25519_key.pub
install -d -o ansible -g ansible -m 0700 /home/ansible/.ssh
install -o ansible -g ansible -m 0600 /authorized_keys /home/ansible/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e -o HostKey=/etc/ssh/ssh_host_ed25519_key
