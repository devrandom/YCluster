FROM docker.io/ubuntu:24.04
RUN apt-get update && apt-get install -y dnsmasq genisoimage
WORKDIR /root
RUN apt-get download shim-signed grub-efi-amd64-signed
RUN dpkg-deb --fsys-tarfile shim-signed*deb | tar x ./usr/lib/shim/shimx64.efi.signed.latest -O > bootx64.efi
RUN dpkg-deb --fsys-tarfile grub-efi-amd64-signed*deb | tar x ./usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed -O > grubx64.efi
COPY config/grub.cfg /root/grub.cfg
COPY startup.sh /root/startup.sh
RUN chmod +x /root/startup.sh
EXPOSE 53 67 69
ENTRYPOINT ["/root/startup.sh"]
