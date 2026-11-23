#!/usr/bin/env bash
# kepler bootstrap: cloud-init user_data and safe to re-run by hand (sudo bash kepler-init.sh).
# Optional: KEYS="<public keys, one per line>" is appended to ubuntu's authorized_keys (a1bot injects deneb's).
# Does NOT join the tailnet; the user runs `sudo tailscale up --hostname kepler` (needs an auth key).
set -u
exec >>/var/log/kepler-init.log 2>&1
echo "=== kepler-init $(date -Is)"
export DEBIAN_FRONTEND=noninteractive
step() { echo "--- $*"; }

step hostname/timezone
hostnamectl set-hostname kepler
grep -q '127.0.1.1 kepler' /etc/hosts || echo '127.0.1.1 kepler' >> /etc/hosts
timedatectl set-timezone Asia/Seoul

step packages
apt-get update -y && apt-get upgrade -y
apt-get install -y git curl ca-certificates python3 docker.io docker-compose-v2 fail2ban unattended-upgrades \
  prometheus-node-exporter iptables-persistent
systemctl enable --now docker fail2ban prometheus-node-exporter
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable tailscaled   # installed, not joined

step ubuntu user and ssh keys
id ubuntu >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo,docker ubuntu
usermod -aG docker ubuntu
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
touch /home/ubuntu/.ssh/authorized_keys
printf '%s\n' "${KEYS:-}" | while read -r k; do
  [ -n "$k" ] && ! grep -qxF "$k" /home/ubuntu/.ssh/authorized_keys && echo "$k" >> /home/ubuntu/.ssh/authorized_keys
done
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys; chmod 600 /home/ubuntu/.ssh/authorized_keys
cat > /etc/ssh/sshd_config.d/00-kepler.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
CONF
sshd -t && systemctl reload ssh

step "firewall: public ssh only, tailscale0 open (node-exporter via tailnet)"
ipt() { iptables -C INPUT "$@" 2>/dev/null || iptables -I INPUT 1 "$@"; }
ipt -i tailscale0 -j ACCEPT
ipt -p tcp --dport 22 -j ACCEPT
ipt -m state --state RELATED,ESTABLISHED -j ACCEPT
ipt -i lo -j ACCEPT
netfilter-persistent save

step motd
cat > /etc/motd <<'MOTD'

  kepler: Azel primary gateway candidate (OCI Chuncheon, A1 2 OCPU / 12 GB)
  Not joined to the tailnet until: sudo tailscale up --hostname kepler

MOTD
echo "=== done $(date -Is)"
