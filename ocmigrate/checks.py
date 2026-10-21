"""Migration-readiness checks: would this workload meet its continuity targets once
it is on OpenShift? Errors block the migration wave; warnings need a sign-off."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .inventory import App
from .policy import POLICY

SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|api_?key|private_?key)")


@dataclass(frozen=True)
class Finding:
    app: str
    severity: str  # ERROR | WARN
    code: str
    message: str


def check(apps: list[App]) -> list[Finding]:
    by_name = {a.name: a for a in apps}
    out: list[Finding] = []
    for a in apps:
        pol = POLICY[a.tier]

        def add(sev, code, msg, a=a):
            out.append(Finding(a.name, sev, code, msg))

        if a.rto_minutes > pol.max_rto_minutes:
            add("ERROR", "RTO_TIER", f"RTO {a.rto_minutes}m exceeds tier {a.tier} maximum {pol.max_rto_minutes}m")
        if a.rpo_minutes > pol.max_rpo_minutes:
            add("ERROR", "RPO_TIER", f"RPO {a.rpo_minutes}m exceeds tier {a.tier} maximum {pol.max_rpo_minutes}m")
        # Stateful single-writer apps get their resilience from replicated storage
        # (checked below), not from extra pods on a ReadWriteOnce volume.
        if a.kind == "service" and not a.storage and a.replicas < pol.min_replicas:
            add("ERROR", "REPLICAS", f"tier {a.tier} needs >= {pol.min_replicas} replicas, has {a.replicas}")

        if a.storage:
            s = a.storage
            if not s.replicated:
                if s.backup_interval_minutes is None:
                    add("ERROR", "NO_BACKUP", "stateful app has neither replication nor scheduled backups")
                elif s.backup_interval_minutes > a.rpo_minutes:
                    add("ERROR", "RPO_UNACHIEVABLE",
                        f"backups every {s.backup_interval_minutes}m cannot meet RPO {a.rpo_minutes}m without replication")
                if a.tier == 1:
                    add("ERROR", "TIER1_UNREPLICATED",
                        "tier 1 stateful data must be replicated to the DR site; restore-from-backup cannot meet a 15m RTO")
            if a.kind == "service" and a.replicas > 1:
                add("WARN", "RWO_REPLICAS", "ReadWriteOnce volume with >1 replica: only one pod can mount it")

        tag = a.image.rsplit("/", 1)[-1]
        if "@sha256:" not in a.image and (":" not in tag or tag.endswith(":latest")):
            add("WARN", "MUTABLE_IMAGE", f"image {a.image} is not pinned to a version tag or digest")

        if a.cutover_minutes > a.rto_minutes:
            add("WARN", "CUTOVER_EXCEEDS_RTO",
                f"estimated cutover {a.cutover_minutes}m exceeds RTO {a.rto_minutes}m; run blue/green in parallel "
                "and switch traffic, rather than stop-and-move")

        for dep in a.depends_on:
            d = by_name[dep]
            if d.tier > a.tier:
                add("ERROR", "CRITICALITY_INVERSION",
                    f"tier {a.tier} app depends on tier {d.tier} app {dep}; its RTO is bounded by {dep}'s "
                    f"{d.rto_minutes}m")

        for k, v in a.env.items():
            if SECRET_KEY.search(k) and v:
                add("ERROR", "LITERAL_SECRET", f"env {k} looks like a credential; move it to a Secret")
    return out


def blocking(findings: list[Finding]) -> bool:
    return any(f.severity == "ERROR" for f in findings)
