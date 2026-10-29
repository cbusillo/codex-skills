ARG GUEST_IMAGE=alpine:3.24
FROM ${GUEST_IMAGE} AS guest
RUN apk add --no-cache linux-virt docker docker-cli-compose python3 iptables
COPY init /init
COPY guest.py /native/guest.py
RUN chmod 755 /init && mkdir -p /workspace /var/lib/docker /sys/fs/cgroup /native/control

FROM alpine:3.24 AS packed
RUN apk add --no-cache cpio gzip
COPY --from=guest / /rootfs/
RUN mkdir /native && mv /rootfs/boot/vmlinuz-virt /native/vmlinuz && \
    rm -rf /rootfs/boot && mknod /rootfs/dev/console c 5 1 && \
    cd /rootfs && find . -print0 | cpio --null -o -H newc | gzip -1 > /native/initramfs.gz

FROM alpine:3.24
RUN apk add --no-cache qemu-system-x86_64 python3 e2fsprogs
COPY --from=packed /native/ /native/
COPY run.py /native/run.py
ENTRYPOINT ["python3", "/native/run.py"]
CMD ["sh", "/workspace/run-native.sh"]
