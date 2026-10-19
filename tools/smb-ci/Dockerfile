FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends samba && rm -rf /var/lib/apt/lists/* \
    && useradd --no-create-home smbtest && mkdir /share && chown smbtest:smbtest /share \
    && printf '%s\n%s\n' 'smb-ci-only' 'smb-ci-only' | smbpasswd -a -s smbtest
COPY smb.conf /etc/samba/smb.conf
CMD ["smbd", "--foreground", "--no-process-group", "--debug-stdout"]
