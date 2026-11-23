#!/usr/bin/env python3
"""Keep trying to launch ONE Always Free Ampere A1 instance (2 OCPU / 12 GB) in OCI Chuncheon.

Commands: run | dry-run | status | selfcheck.  Reads ~/.oci/config; never prints key material.
"""
import base64
import hashlib
import json
import logging
import logging.handlers
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SHAPE, OCPUS, MEM_GB = "VM.Standard.A1.Flex", 2, 12  # the only thing this bot may launch
NAME, REF_NAME = "kepler", "main-amd"  # new instance name; instance whose subnet/keys we copy
FREE_OCPUS, FREE_MEM, FREE_STORAGE = 2, 12, 200  # Always Free caps (A1 total, boot+block GB)
BOOT_WANT, BOOT_MIN = 50, 47
BASE_S, JITTER_S, BACKOFF_MAX_S = 60, 20, 900
WATCH_S, WATCH_JITTER_S = 3600, 300  # limit check interval while A1 availability is 0
NTFY_URL, NTFY_TOKEN = "http://100.64.0.6:8090/infra", Path.home() / ".ntfy_token"  # same path the old a1bot used
CALL_URL, AZEL_TOKEN = "https://azel.scian.xyz/v1/calls", Path.home() / ".config/azel/token"
INIT = Path(__file__).with_name("kepler-init.sh")
STATE_DIR = Path.home() / ".local/state/a1bot"
STATE, DONE, LOGFILE = STATE_DIR / "state.json", STATE_DIR / "DONE", STATE_DIR / "a1bot.log"
LIVE = ("PROVISIONING", "AVAILABLE", "RESTORING", "CREATING_IMAGE", "STARTING", "RUNNING", "STOPPING", "STOPPED")

log = logging.getLogger("a1bot")


def guard(a1_ocpus, a1_mem, storage_gb, boot_gb, ocpus=OCPUS, mem=MEM_GB):
    """Return (ok, reason) for launching ocpus/mem/boot_gb on top of what already exists."""
    if a1_ocpus + ocpus > FREE_OCPUS:
        return False, f"OCPU {a1_ocpus}+{ocpus} > {FREE_OCPUS}"
    if a1_mem + mem > FREE_MEM:
        return False, f"memory {a1_mem}+{mem} > {FREE_MEM}"
    if storage_gb + boot_gb > FREE_STORAGE:
        return False, f"storage {storage_gb}+{boot_gb} > {FREE_STORAGE}"
    return True, "ok"


def fit(avail_cores, avail_mem, a1_ocpus, a1_mem):
    """Largest (ocpus, mem) that the tenancy limit and the free-tier caps both allow; None if under 1 OCPU."""
    o = min(OCPUS, FREE_OCPUS - a1_ocpus, int(avail_cores))
    m = min(MEM_GB, FREE_MEM - a1_mem, int(avail_mem), o * 6)
    return (o, m) if o >= 1 and m >= o else None


def boot_size(storage_gb):
    """50 GB, or the remainder if smaller; None when under the 47 GB minimum."""
    size = min(BOOT_WANT, FREE_STORAGE - storage_gb)
    return size if size >= BOOT_MIN else None


def selfcheck():
    assert guard(0, 0, 100, 50) == (True, "ok")
    assert guard(0, 0, 150, 50)[0] and not guard(0, 0, 151, 50)[0]  # exactly 200 is fine
    assert not guard(1, 6, 0, 50)[0] and not guard(0, 1, 0, 50)[0]  # any existing A1 blocks 2/12
    assert boot_size(94) == 50 and boot_size(153) == 47 and boot_size(154) is None
    assert boot_size(100) == 50 and boot_size(0) == 50
    assert fit(0, 0, 0, 0) is None and fit(4, 24, 0, 0) == (2, 12) and fit(1, 6, 0, 0) == (1, 6)
    assert fit(2, 12, 1, 6) == (1, 6) and fit(2, 12, 2, 12) is None and fit(2, 3, 0, 0) == (2, 3)
    print("selfcheck ok")


def setup_log():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%F %T")
    for h in (logging.handlers.RotatingFileHandler(LOGFILE, maxBytes=500_000, backupCount=1),
              logging.StreamHandler(sys.stdout)):  # stdout -> journal (journal adds its own timestamp, harmless)
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"attempts": 0, "capacity": 0, "throttled": 0, "errors": 0, "first_started": time.time()}


def save_state(s):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(STATE)


def status():
    s = load_state()
    if DONE.exists():
        print("DONE:", DONE.read_text().strip())
    up = time.time() - s.get("started", s["first_started"])
    print(f"mode {s.get('mode', '-')}; last check {s.get('last_check', '-')}; limit cores {s.get('limit_cores', '-')} mem {s.get('limit_mem', '-')}")
    print(f"attempts {s['attempts']} (capacity {s['capacity']}, throttled {s['throttled']}, other errors {s['errors']})")
    print(f"last attempt: {s.get('last_at', '-')} fd={s.get('last_fd', '-')}")
    print(f"last error: {s.get('last_error', '-')}")
    print(f"since service start: {int(up // 3600)}h{int(up % 3600 // 60):02d}m")


class Oci:
    def __init__(self):
        import oci
        self.oci = oci
        cfg = oci.config.from_file()
        self.cfg = cfg
        self.tenancy = cfg["tenancy"]
        self.compute = oci.core.ComputeClient(cfg)
        self.net = oci.core.VirtualNetworkClient(cfg)
        self.bs = oci.core.BlockstorageClient(cfg)
        self.ident = oci.identity.IdentityClient(cfg)

    def compartments(self):
        r = self.ident.list_compartments(self.tenancy, compartment_id_in_subtree=True, access_level="ACCESSIBLE",
                                         lifecycle_state="ACTIVE")
        return [self.tenancy] + [c.id for c in r.data]

    def usage(self):
        """A1 OCPUs, A1 memory GB and boot+block GB over every compartment and AD."""
        o = self.oci
        ocpus = mem = store = 0
        ads = [a.name for a in self.ident.list_availability_domains(self.tenancy).data]
        for c in self.compartments():
            for i in o.pagination.list_call_get_all_results(self.compute.list_instances, compartment_id=c).data:
                if i.lifecycle_state in LIVE and i.shape == SHAPE:
                    ocpus += i.shape_config.ocpus
                    mem += i.shape_config.memory_in_gbs
            for ad in ads:
                for b in o.pagination.list_call_get_all_results(self.bs.list_boot_volumes, availability_domain=ad, compartment_id=c).data:
                    if b.lifecycle_state in LIVE:
                        store += b.size_in_gbs
            for v in o.pagination.list_call_get_all_results(self.bs.list_volumes, compartment_id=c).data:
                if v.lifecycle_state in LIVE:
                    store += v.size_in_gbs
        return ocpus, mem, store

    def limits(self):
        """Available A1 cores and memory GB in the AD (tenancy limit minus use)."""
        ad = self.ident.list_availability_domains(self.tenancy).data[0].name
        lim = self.oci.limits.LimitsClient(self.cfg)
        g = lambda n: lim.get_resource_availability("compute", n, self.tenancy, availability_domain=ad).data.available
        return g("standard-a1-core-count"), g("standard-a1-memory-count")

    def reference(self):
        """Subnet, AD and authorized keys of the REF_NAME instance (searched in every compartment)."""
        o = self.oci
        for c in self.compartments():
            for i in o.pagination.list_call_get_all_results(self.compute.list_instances, compartment_id=c).data:
                if i.display_name == REF_NAME and i.lifecycle_state == "RUNNING":
                    att = self.compute.list_vnic_attachments(i.compartment_id, instance_id=i.id).data[0]
                    keys = (self.compute.get_instance(i.id).data.metadata or {}).get("ssh_authorized_keys", "")
                    return i, att.subnet_id, keys
        raise SystemExit(f"reference instance {REF_NAME} not found")

    def image(self):
        r = self.compute.list_images(self.tenancy, operating_system="Canonical Ubuntu", operating_system_version="24.04",
                                     shape=SHAPE, sort_by="TIMECREATED", sort_order="DESC")
        for im in r.data:  # newest first; the shape filter already limits this to images that boot on A1
            if "aarch64" in im.display_name and "Minimal" not in im.display_name:
                return im
        raise SystemExit("no Ubuntu 24.04 aarch64 image found")

    def launch(self, ref, subnet, keys, image_id, boot_gb, fd, ocpus, mem):
        m = self.oci.core.models
        d = m.LaunchInstanceDetails(
            compartment_id=ref.compartment_id, availability_domain=ref.availability_domain, fault_domain=fd,
            display_name=NAME, shape=SHAPE, shape_config=m.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=mem),
            source_details=m.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=boot_gb),
            create_vnic_details=m.CreateVnicDetails(subnet_id=subnet, assign_public_ip=True),
            metadata={"ssh_authorized_keys": keys, "user_data": user_data(keys)})
        return self.compute.launch_instance(d).data

    def public_ip(self, inst):
        for _ in range(30):
            att = [a for a in self.compute.list_vnic_attachments(inst.compartment_id, instance_id=inst.id).data
                   if a.lifecycle_state == "ATTACHED"]
            if att:
                ip = self.net.get_vnic(att[0].vnic_id).data.public_ip
                if ip:
                    return ip
            time.sleep(10)
        return "unknown"


def user_data(keys):
    """kepler-init.sh with deneb's public keys prepended, base64 for cloud-init."""
    script = "#!/usr/bin/env bash\nexport KEYS=%s\n" % json.dumps(keys.strip()) + INIT.read_text().split("\n", 1)[1]
    return base64.b64encode(script.encode()).decode()


def key_summary(keys):
    out = []
    for line in keys.strip().splitlines():
        p = line.split()
        if len(p) >= 2:
            out.append(f"{p[0]} sha256:{hashlib.sha256(p[1].encode()).hexdigest()[:16]} {p[2] if len(p) > 2 else ''}".strip())
    return out


def resolve(c):
    """Everything a launch needs plus the free-tier numbers; shared by dry-run and run."""
    ref, subnet, keys = c.reference()
    im = c.image()
    ocpus, mem, store = c.usage()
    boot = boot_size(store)
    return ref, subnet, keys, im, ocpus, mem, store, boot


def dry_run():
    c = Oci()
    ref, subnet, keys, im, ocpus, mem, store, boot = resolve(c)
    print(f"region      {c.cfg['region']}  (compartments scanned: {len(c.compartments())})")
    print(f"reference   {ref.display_name} {ref.shape} AD {ref.availability_domain} FDs tried: none + 1,2,3")
    print(f"image       {im.display_name}\n            {im.id}")
    print(f"subnet      {subnet}")
    print(f"ssh keys    {len(key_summary(keys))}: {key_summary(keys)}")
    print(f"existing    A1 {ocpus} OCPU / {mem} GB; boot+block storage {store} GB of {FREE_STORAGE}")
    print(f"boot volume {boot} GB (remainder {FREE_STORAGE - store}, want {BOOT_WANT}, min {BOOT_MIN})")
    cores, lmem = c.limits()
    sh = fit(cores, lmem, ocpus, mem) or (OCPUS, MEM_GB)
    print(f"A1 limit    available {cores} cores / {lmem} GB -> {'would launch ' + str(sh) if fit(cores, lmem, ocpus, mem) else 'watch mode (no launch)'}")
    ok, why = guard(ocpus, mem, store, boot or 0, *sh) if boot else (False, "storage remainder < 47 GB")
    print(f"launch      {SHAPE} {sh[0]} OCPU / {sh[1]} GB '{NAME}' public IP -> guard: {'PASS' if ok else 'FAIL ' + why}")
    print(f"cloud-init  kepler-init.sh {len(base64.b64decode(user_data(keys)))} bytes (oracle limit 32768)")
    try:
        print(f"azel token  {AZEL_TOKEN} mode {oct(AZEL_TOKEN.stat().st_mode & 0o777)} size {AZEL_TOKEN.stat().st_size}")
    except OSError:
        print(f"azel token  MISSING at {AZEL_TOKEN}; the Azel call would be skipped")
    for what, url, method in (("azel endpoint", CALL_URL, "OPTIONS"), ("ntfy endpoint", NTFY_URL, "OPTIONS")):
        try:  # unauthenticated OPTIONS never rings; any HTTP answer proves reachability
            print(f"{what} {method} -> {urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=10).status}")
        except urllib.error.HTTPError as e:
            print(f"{what} {method} -> HTTP {e.code} (reachable)")
        except Exception as e:
            print(f"{what} {method} -> {type(e).__name__}")
    print(f"ntfy token  {'present' if NTFY_TOKEN.exists() else 'MISSING'}")
    return 0 if ok else 1


def post(url, body, headers, retries=3):
    for n in range(retries):
        try:
            req = urllib.request.Request(url, body, headers)
            return urllib.request.urlopen(req, timeout=15).status
        except urllib.error.HTTPError as e:
            return e.code  # server answered; do not retry (could ring twice)
        except Exception:
            time.sleep(5 * (n + 1))
    return "network error"


def ntfy(title, body):
    r = ntfy("kepler 생성됨", info)
    log.info("ntfy: %s", r)
    try:
        tok = AZEL_TOKEN.read_text().strip()
        reason = f"춘천 A1 서버 kepler가 만들어졌어요. 공인 IP {ip}, {ocpus} OCPU {mem}GB.{small}"[:200]
        r = post(CALL_URL, json.dumps({"reason": reason}).encode(),
                 {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    except OSError:
        r = "no token"
    log.info("azel call: %s", r)


def run():
    if DONE.exists():
        log.info("done marker present, nothing to do")
        return 0
    c = Oci()
    st = load_state()
    st["started"] = time.time()
    fds = [None] + [f"FAULT-DOMAIN-{n}" for n in (1, 2, 3)]
    backoff = 0
    bad = 0  # consecutive auth/lookup errors
    while True:
        fd = fds[st["attempts"] % len(fds)]
        try:
            ref, subnet, keys, im, ocpus, mem, store, boot = resolve(c)
            cores, lmem = c.limits()
            shape = fit(cores, lmem, ocpus, mem)
            st.update(last_check=time.strftime("%F %T"), limit_cores=cores, limit_mem=lmem)
            if not shape:
                st["mode"] = "watch"
                wait = WATCH_S + random.uniform(0, WATCH_JITTER_S)
                log.info("watch: A1 available %s cores / %s GB (in use %s/%s); no launch; next check in %ds", cores, lmem, ocpus, mem, wait)
                save_state(st)
                time.sleep(wait)
                continue
            if st.get("mode") != "launch":
                st["mode"] = "launch"
                if not st.get("limit_notified"):
                    st["limit_notified"] = True
                    log.info("ntfy: %s", ntfy("A1 한도 생김", "A1 한도가 생겼어요, 생성 시도 시작"))
            st["attempts"] += 1
            st["last_at"], st["last_fd"] = time.strftime("%F %T"), fd or "none"
            ok, why = guard(ocpus, mem, store, boot or 0, *shape) if boot else (False, f"storage remainder {FREE_STORAGE - store} GB < {BOOT_MIN}")
            if not ok:
                log.info("STOP guard: %s", why)
                st["last_error"] = "guard: " + why
                save_state(st)
                return 3  # not retried by on-failure? exit code 3 is a failure -> see unit (RestartPreventExitStatus=3)
            inst = c.launch(ref, subnet, keys, im.id, boot, fd, *shape)
            ip = c.public_ip(inst)
            DONE.write_text(f"{inst.id} {ip} {time.strftime('%F %T')}\n")
            log.info("SUCCESS %s ip %s", inst.id, ip)
            print(f"instance {inst.id}\npublic ip {ip}")
            notify(ip, boot, *shape)
            save_state(st)
            return 0
        except c.oci.exceptions.ServiceError as e:
            msg = (e.message or "").strip()
            if e.status == 429:
                st["throttled"] += 1
                backoff = min(max(backoff * 2, BASE_S), BACKOFF_MAX_S)
                err, bad = "429 TooManyRequests", 0
            elif e.status == 500 and "capacity" in msg.lower():
                st["capacity"] += 1
                backoff, err, bad = 0, "Out of host capacity", 0
            else:
                st["errors"] += 1
                backoff = max(backoff, 300)
                err = f"{e.status} {e.code}: {msg[:80]}"
                bad = bad + 1 if e.status in (401, 403, 404) else 0
        except Exception as e:  # network blips etc.; keep going
            st["errors"] += 1
            backoff = max(backoff, 120)
            err = f"{type(e).__name__}: {str(e)[:80]}"
        st["last_error"] = err
        save_state(st)
        wait = backoff or BASE_S + random.uniform(0, JITTER_S)
        log.info("attempt %d fd=%s: %s; next in %ds", st["attempts"], fd or "none", err, wait)
        if bad >= 5:
            log.info("auth/lookup errors x5, exiting for systemd to retry later")
            return 2
        time.sleep(wait)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "selfcheck":
        selfcheck()
    elif cmd == "status":
        status()
    elif cmd in ("run", "dry-run"):
        setup_log()
        sys.exit(run() if cmd == "run" else dry_run())
    else:
        sys.exit("usage: bot.py run|dry-run|status|selfcheck")
