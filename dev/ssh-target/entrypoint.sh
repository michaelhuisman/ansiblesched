#!/bin/sh
set -eu
ssh-keygen -A
install -d -o ansible -g ansible -m 0700 /home/ansible/.ssh
install -o ansible -g ansible -m 0600 /authorized_keys /home/ansible/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e
