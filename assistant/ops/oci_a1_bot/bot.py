#!/usr/bin/env python3
"""Keep trying to launch ONE Always Free Ampere A1 instance (2 OCPU / 12 GB) in OCI Chuncheon.

Commands: run | dry-run | status | selfcheck.  Reads ~/.oci/config; never prints key material.
"""
import hashlib
import json
import logging
import logging.handlers
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

SHAPE, OCPUS, MEM_GB = "VM.Standard.A1.Flex", 2, 12  # the only thing this bot may launch
NAME, REF_NAME = "chuncheon-a1", "main-amd"  # new instance name; instance whose subnet/keys we copy
FREE_OCPUS, FREE_MEM, FREE_STORAGE = 2, 12, 200  # Always Free caps (A1 total, boot+block GB)
BOOT_WANT, BOOT_MIN = 50, 47
BASE_S, JITTER_S, BACKOFF_MAX_S = 60, 20, 900
NTFY_URL, NTFY_TOKEN = "http://100.64.0.6:8090/infra", Path.home() / ".ntfy_token"  # same path the old a1bot used
STATE_DIR = Path.home() / ".local/state/a1bot"
STATE, DONE, LOGFILE = STATE_DIR / "state.json", STATE_DIR / "DONE", STATE_DIR / "a1bot.log"
LIVE = ("PROVISIONING", "AVAILABLE", "RESTORING", "CREATING_IMAGE", "STARTING", "RUNNING", "STOPPING", "STOPPED")

log = logging.getLogger("a1bot")


def guard(a1_ocpus, a1_mem, storage_gb, boot_gb):
    """Return (ok, reason) for launching OCPUS/MEM_GB/boot_gb on top of what already exists."""
    if a1_ocpus + OCPUS > FREE_OCPUS:
        return False, f"OCPU {a1_ocpus}+{OCPUS} > {FREE_OCPUS}"
    if a1_mem + MEM_GB > FREE_MEM:
        return False, f"memory {a1_mem}+{MEM_GB} > {FREE_MEM}"
    if storage_gb + boot_gb > FREE_STORAGE:
        return False, f"storage {storage_gb}+{boot_gb} > {FREE_STORAGE}"
    return True, "ok"


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

    def launch(self, ref, subnet, keys, image_id, boot_gb, fd):
        m = self.oci.core.models
        d = m.LaunchInstanceDetails(
            compartment_id=ref.compartment_id, availability_domain=ref.availability_domain, fault_domain=fd,
            display_name=NAME, shape=SHAPE, shape_config=m.LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEM_GB),
            source_details=m.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=boot_gb),
            create_vnic_details=m.CreateVnicDetails(subnet_id=subnet, assign_public_ip=True),
            metadata={"ssh_authorized_keys": keys})
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
    ok, why = guard(ocpus, mem, store, boot or 0) if boot else (False, "storage remainder < 47 GB")
    print(f"launch      {SHAPE} {OCPUS} OCPU / {MEM_GB} GB '{NAME}' public IP -> guard: {'PASS' if ok else 'FAIL ' + why}")
    return 0 if ok else 1


def notify(title, body):
    try:
        tok = NTFY_TOKEN.read_text().strip()
        req = urllib.request.Request(NTFY_URL, body.encode(), {"Authorization": f"Bearer {tok}", "Title": title,
                                                              "Priority": "max", "Tags": "rocket"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # never let a failed notification lose the success
        log.info("notify failed: %s", type(e).__name__)


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
        st["attempts"] += 1
        st["last_at"], st["last_fd"] = time.strftime("%F %T"), fd or "none"
        try:
            ref, subnet, keys, im, ocpus, mem, store, boot = resolve(c)
            ok, why = guard(ocpus, mem, store, boot or 0) if boot else (False, f"storage remainder {FREE_STORAGE - store} GB < {BOOT_MIN}")
            if not ok:
                log.info("STOP guard: %s", why)
                st["last_error"] = "guard: " + why
                save_state(st)
                return 3  # not retried by on-failure? exit code 3 is a failure -> see unit (RestartPreventExitStatus=3)
            inst = c.launch(ref, subnet, keys, im.id, boot, fd)
            ip = c.public_ip(inst)
            DONE.write_text(f"{inst.id} {ip} {time.strftime('%F %T')}\n")
            log.info("SUCCESS %s ip %s", inst.id, ip)
            print(f"instance {inst.id}\npublic ip {ip}")
            notify("A1 launched", f"chuncheon-a1 (2 OCPU/12 GB/{boot} GB) created: {ip}")
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
