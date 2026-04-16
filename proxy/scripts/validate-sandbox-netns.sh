#!/usr/bin/env bash
# validate-sandbox-netns.sh — one-shot host readiness check for the (always-on)
# sandbox network isolation.
#
# Sandbox isolation is mandatory; the proxy hard-fails boot if the host can't
# enforce it. Run this to diagnose such a host: it exercises the real
# launcher -> pasta -> shim -> bwrap chain (no proxy / DB / MCP containers
# needed) and reports PASS/FAIL for each property the design depends on:
#
#   1. environment facts (kernel, bwrap setuid bit, pasta version, resolver,
#      proxy uid) — the setuid-bwrap-inside-pasta-userns case is THE risk;
#   2. an allow-listed loopback port is reachable from inside the netns
#      (pasta -T forwarding — stands in for the proxy hook port + Docker MCPs);
#   3. a NON-allow-listed loopback port is refused (Postgres :5432 stand-in);
#   4. the cloud-metadata IP 169.254.169.254 is unreachable (blackhole route);
#   5. outbound DNS + internet work (NAT + --dns-forward on stub-resolver hosts);
#   6. files the agent writes are host-owned by the proxy uid (userns map-back);
#   7. the in-sandbox uid matches the proxy uid (no surprise root).
#
# Exit 0 iff every check passes. Safe + read-only on the host (only writes a
# couple of temp files it cleans up).

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER="$HERE/oto-sandbox-net"
PASS=0 FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
info() { printf '  ---- %s\n' "$1"; }

echo "== oto-sandbox-net validation =="

# --- 0. prerequisites ----------------------------------------------------
need_abort=0
for t in pasta bwrap ip python3; do
    command -v "$t" >/dev/null 2>&1 || { echo "MISSING: $t"; need_abort=1; }
done
[ -x "$LAUNCHER" ] || { echo "MISSING (not executable): $LAUNCHER"; need_abort=1; }
if [ "$need_abort" = 1 ]; then
    echo "Install the missing tools first (passt provides pasta; see VERSIONS.md)."
    exit 2
fi

# --- 1. environment facts -----------------------------------------------
echo "[1] environment"
info "kernel:        $(uname -r)"
BWRAP_PATH="$(command -v bwrap)"
BWRAP_PERMS="$(stat -c '%A' "$BWRAP_PATH")"
info "bwrap:         $BWRAP_PATH ($BWRAP_PERMS)"
case "$BWRAP_PERMS" in
    *s*) info "bwrap is SETUID — the userns-nesting risk APPLIES; if checks below"
         info "  fail, the launcher's O1->O2 swap is the contained fix." ;;
    *)   info "bwrap is non-setuid (unprivileged-userns variant — nests cleanly)." ;;
esac
info "pasta:         $(pasta --version 2>&1 | head -1)"
PROXY_UID="$(id -u)"; PROXY_GID="$(id -g)"
info "proxy uid:gid: $PROXY_UID:$PROXY_GID  ($([ "$PROXY_UID" = 0 ] && echo 'rootful pasta — no userns nesting' || echo 'rootless pasta — userns map-back expected'))"

# --- DNS: mirror the proxy's stub-resolver handling ----------------------
DNS_ARGS=""
RESOLV_BIND=""
TMPRESOLV=""
if grep -Eq '^\s*nameserver\s+127\.' /etc/resolv.conf 2>/dev/null; then
    TMPRESOLV="$(mktemp)"
    echo "nameserver 169.254.1.1" > "$TMPRESOLV"
    DNS_ARGS="--dns-forward 169.254.1.1"
    RESOLV_BIND="--ro-bind $TMPRESOLV /etc/resolv.conf"
    info "resolver:      loopback stub -> using --dns-forward 169.254.1.1 + generated resolv.conf"
else
    RESOLV_BIND="--ro-bind /etc/resolv.conf /etc/resolv.conf"
    info "resolver:      non-loopback -> host /etc/resolv.conf bound directly"
fi

# --- 2-7. one netns run, many probes ------------------------------------
echo "[2-7] launching netns chain + probing"

WS="$(mktemp -d)"
# Free ports for the allow-listed + blocked host listeners.
read OK_PORT BLOCKED_PORT <<EOF
$(python3 - <<'PY'
import socket
def free():
    s=socket.socket(); s.bind(("127.0.0.1",0)); p=s.getsockname()[1]; s.close(); return p
print(free(), free())
PY
)
EOF

# Host listeners (init namespace). The OK one is forwarded; the BLOCKED one
# is not, so an in-netns connect to it must be refused.
python3 - "$OK_PORT" <<'PY' &
import socket,sys
srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
srv.bind(("127.0.0.1",int(sys.argv[1]))); srv.listen(1)
try:
    c,_=srv.accept(); c.sendall(b"REACHED"); c.close()
except OSError: pass
PY
OK_LISTENER=$!
python3 - "$BLOCKED_PORT" <<'PY' &
import socket,sys
srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
srv.bind(("127.0.0.1",int(sys.argv[1]))); srv.listen(1)
try:
    c,_=srv.accept(); c.sendall(b"LEAK"); c.close()
except OSError: pass
PY
BLOCKED_LISTENER=$!
# Give the listeners a moment to bind.
python3 -c 'import time; time.sleep(0.4)'

# The in-netns probe: connectivity + DNS + file ownership + uid. Emits
# KEY=VALUE lines we grep below.
PROBE=$(cat <<PY
import os,socket,subprocess,sys
def chk(p):
    s=socket.socket(); s.settimeout(3)
    try:
        s.connect(("127.0.0.1",p)); d=s.recv(16); s.close(); return d.decode()
    except Exception as e:
        return "ERR:"+type(e).__name__
print("OK_CONN="+chk($OK_PORT))
print("BLOCKED_CONN="+chk($BLOCKED_PORT))
# Route-level reachability of the host's private service IPs. pasta NATs
# outbound host-side, so these must be blackholed (ip route get returns
# non-zero when unreachable). The Docker bridge gateway is the concrete leak
# this guards (host Postgres is often reachable on 172.17.0.1).
def routable(ip):
    return subprocess.run(["ip","route","get",ip],
                          capture_output=True,text=True).returncode == 0
print("META_ROUTE="+("up" if routable("169.254.169.254") else "blocked"))
print("DOCKERGW_ROUTE="+("up" if routable("172.17.0.1") else "blocked"))
print("RFC1918_10_ROUTE="+("up" if routable("10.255.255.254") else "blocked"))
print("RFC1918_192_ROUTE="+("up" if routable("192.168.255.254") else "blocked"))
try:
    socket.getaddrinfo("example.com",443); print("DNS=ok")
except Exception as e:
    print("DNS=ERR:"+type(e).__name__)
# Outbound NAT to the public internet (raw IP, no DNS) — confirms pasta
# --config-net routes egress and that -t/-u/-U none did not gate it.
def out(ip):
    s=socket.socket(); s.settimeout(5)
    try:
        s.connect((ip,443)); s.close(); return "ok"
    except Exception as e:
        return "ERR:"+type(e).__name__
print("OUTBOUND="+out("1.1.1.1"))
print("UID="+str(os.getuid()))
open("/workspace/probe.txt","w").write("hi")
print("WROTE=ok")
PY
)

# bwrap argv: minimal-but-faithful (system RO mounts + writable workspace),
# uid map-back when rootless — exactly what SandboxBuilder emits under the flag.
UID_FLAGS=""
[ "$PROXY_UID" != 0 ] && UID_FLAGS="--unshare-user --uid $PROXY_UID --gid $PROXY_GID"

# shellcheck disable=SC2086
"$LAUNCHER" --block-private --forward "$OK_PORT" $DNS_ARGS -- \
    bwrap --unshare-pid --die-with-parent --share-net $UID_FLAGS \
        --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib \
        --ro-bind /sbin /sbin $( [ -d /lib64 ] && echo --ro-bind /lib64 /lib64 ) \
        --ro-bind /etc/ssl /etc/ssl --ro-bind /etc/hosts /etc/hosts \
        --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
        $RESOLV_BIND \
        --dev /dev --proc /proc --tmpfs /tmp \
        --bind "$WS" /workspace --chdir /workspace \
        -- python3 -c "$PROBE" > "$WS/out.txt" 2>"$WS/err.txt"
RC=$?

OUT="$(cat "$WS/out.txt" 2>/dev/null)"
[ $RC -eq 0 ] || { info "chain exited $RC; stderr:"; sed 's/^/      /' "$WS/err.txt"; }

grep -q 'OK_CONN=REACHED'   <<<"$OUT" && ok "allow-listed port reachable via pasta -T" \
                                       || bad "allow-listed port NOT reachable (OK_CONN: $(grep OK_CONN <<<"$OUT"))"
grep -q 'BLOCKED_CONN=ERR:' <<<"$OUT" && ok "non-allow-listed loopback port refused (Postgres-class blocked)" \
                                       || bad "non-allow-listed port was REACHABLE — leak ($(grep BLOCKED_CONN <<<"$OUT"))"
grep -q 'META_ROUTE=blocked' <<<"$OUT" && ok "cloud-metadata IP unreachable (blackhole route)" \
                                       || bad "metadata 169.254.169.254 is ROUTABLE — blackhole failed"
grep -q 'DOCKERGW_ROUTE=blocked' <<<"$OUT" && ok "Docker bridge gateway 172.17.0.1 unreachable (host services blocked)" \
                                            || bad "Docker bridge 172.17.0.1 is ROUTABLE — host Postgres-class LEAK (the gap this fix closes)"
grep -q 'RFC1918_10_ROUTE=blocked' <<<"$OUT" && ok "RFC1918 10.0.0.0/8 unreachable" \
                                             || bad "10.0.0.0/8 ROUTABLE — private-range leak"
grep -q 'RFC1918_192_ROUTE=blocked' <<<"$OUT" && ok "RFC1918 192.168.0.0/16 unreachable" \
                                              || bad "192.168.0.0/16 ROUTABLE — private-range leak"
grep -q 'DNS=ok' <<<"$OUT" && ok "DNS resolution works inside the netns" \
                           || bad "DNS failed inside the netns ($(grep '^DNS=' <<<"$OUT")) — if this is the only failure on a stub-resolver host, try dropping '-U none' in oto-sandbox-net"
grep -q 'OUTBOUND=ok' <<<"$OUT" && ok "outbound internet (NAT) works — 1.1.1.1:443 reachable" \
                                || bad "outbound internet failed ($(grep '^OUTBOUND=' <<<"$OUT")) — pasta NAT/egress problem"
# File ownership on the host side.
if [ -f "$WS/probe.txt" ]; then
    OWNER="$(stat -c '%u:%g' "$WS/probe.txt")"
    [ "$OWNER" = "$PROXY_UID:$PROXY_GID" ] && ok "agent-written file is host-owned by proxy uid ($OWNER)" \
                                           || bad "file owner $OWNER != proxy $PROXY_UID:$PROXY_GID"
else
    bad "agent could not write to /workspace"
fi
# In-sandbox uid.
INUID="$(grep '^UID=' <<<"$OUT" | cut -d= -f2)"
if [ "$PROXY_UID" != 0 ]; then
    [ "$INUID" = "$PROXY_UID" ] && ok "in-sandbox uid mapped back to proxy uid ($INUID)" \
                               || bad "in-sandbox uid is $INUID, expected $PROXY_UID (map-back failed)"
else
    info "in-sandbox uid=$INUID (rootful — uid 0 expected, matches today)"
fi

# --- cleanup -------------------------------------------------------------
kill "$OK_LISTENER" "$BLOCKED_LISTENER" 2>/dev/null
rm -rf "$WS"; [ -n "$TMPRESOLV" ] && rm -f "$TMPRESOLV"

echo
echo "== result: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ] || { echo "This host cannot enforce sandbox network isolation — the proxy will hard-fail boot until the failures are understood."; exit 1; }
echo "All checks passed — this host can enforce sandbox network isolation."
