#!/bin/sh
# This script executes inside Nixodria, using the guest's full Rust toolchain.
set -eu
cd /usr/src/nixodria
cargo test --offline
cargo build --release --offline
install -m 755 target/release/nixodria /usr/local/bin/nixodria.new
mv -f /usr/local/bin/nixodria.new /usr/local/bin/nixodria
mkdir -p /usr/local/sbin /usr/share/nixodria/apps /usr/lib/nixodria /root/workspace
# The shipped catalog is managed system data. Installed workspace copies keep
# their edits, while retired catalog sources disappear on the next update.
rm -f /usr/share/nixodria/apps/*.rs
cp apps/*.rs /usr/share/nixodria/apps/
cp guest/selftest.sh /usr/lib/nixodria/selftest.sh
cat > /usr/local/sbin/nixodria-console <<'EOF'
#!/bin/sh
export HOME=/root USER=root TERM=vt100
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /root/workspace
exec /usr/local/bin/nixodria
EOF
chmod 755 /usr/local/sbin/nixodria-console
sed -i '/^ttyS0:/d' /etc/inittab
echo 'ttyS0::respawn:/sbin/getty -n -l /usr/local/sbin/nixodria-console -L ttyS0 115200 vt100' >> /etc/inittab
cat > /etc/os-release <<'EOF'
NAME="Nixodria OS"
ID=nixodria
ID_LIKE=alpine
VERSION_ID=2
PRETTY_NAME="Nixodria OS 2 (Rust)"
HOME_URL="https://github.com/Nixodria/Nixodria-OS"
EOF
printf 'Nixodria OS — Rust development on the guest\n' > /etc/motd
rc-update add cupsd default
apk info -v | sort > /usr/lib/nixodria/packages.txt
rustc --version > /usr/lib/nixodria/toolchain.txt
cargo --version >> /usr/lib/nixodria/toolchain.txt
sh guest/selftest.sh
sync
