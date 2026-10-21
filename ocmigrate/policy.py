"""Business-continuity tier policy: what each criticality tier must look like on
OpenShift, at the primary site and at the DR site."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TierPolicy:
    min_replicas: int
    pdb_min_available: int | None
    zone_spread: str | None  # topologySpreadConstraints whenUnsatisfiable, None = no constraint
    max_rto_minutes: int
    max_rpo_minutes: int
    hpa_max_replicas: int | None
    # Replicas at the DR site: tier 1 runs active-active, tier 2 keeps a warm standby
    # (pilot light), tier 3 is cold (manifests applied, scaled to zero).
    dr_mode: str


POLICY = {
    1: TierPolicy(3, 2, "DoNotSchedule", 15, 5, 12, "active-active"),
    2: TierPolicy(2, 1, "ScheduleAnyway", 60, 60, 6, "warm-standby"),
    3: TierPolicy(1, None, None, 480, 1440, None, "cold"),
}


def dr_replicas(tier: int, primary_replicas: int) -> int:
    mode = POLICY[tier].dr_mode
    if mode == "active-active":
        return primary_replicas
    if mode == "warm-standby":
        return 1
    return 0
