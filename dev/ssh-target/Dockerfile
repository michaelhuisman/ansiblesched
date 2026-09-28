# Only target for integration tests. Key auth with the dev key from scripts/dev-keys.sh.
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash ansible \
    && mkdir -p /run/sshd \
    && printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
        'PermitRootLogin no' 'AllowUsers ansible' > /etc/ssh/sshd_config.d/dev.conf
COPY entrypoint.sh /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["sh", "/entrypoint.sh"]
