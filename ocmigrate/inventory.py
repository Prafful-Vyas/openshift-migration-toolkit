"""Application inventory: what is being migrated, and what continuity it must keep."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class Storage:
    size: str
    storage_class: str
    backup_interval_minutes: int | None = None  # None = no scheduled backup
    replicated: bool = False  # synchronously/asynchronously replicated to the DR site


@dataclass(frozen=True)
class App:
    name: str
    image: str
    tier: int  # 1 = critical (RTO ≤ 15m) · 2 = important · 3 = deferrable
    rto_minutes: int
    rpo_minutes: int
    kind: str = "service"  # service | batch
    port: int | None = None
    replicas: int = 2
    schedule: str | None = None  # cron, batch only
    health_path: str = "/healthz"
    cpu: str = "250m"
    memory: str = "512Mi"
    expose: bool = False  # create an external Route
    depends_on: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    secrets: tuple[str, ...] = ()  # names of pre-provisioned Secrets mounted as env
    storage: Storage | None = None
    cutover_minutes: int = 10  # estimated hands-on time for this app's cutover

    @property
    def stateful(self) -> bool:
        return self.storage is not None


def load(path: str | Path) -> list[App]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    apps = []
    for raw in data.get("apps", []):
        raw = dict(raw)
        storage = raw.pop("storage", None)
        try:
            app = App(
                **{**raw,
                   "depends_on": tuple(raw.get("depends_on", ())),
                   "secrets": tuple(raw.get("secrets", ())),
                   "storage": Storage(**storage) if storage else None},
            )
        except TypeError as exc:
            raise InventoryError(f"app {raw.get('name', '?')}: {exc}") from exc
        _validate(app)
        apps.append(app)
    names = [a.name for a in apps]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise InventoryError(f"duplicate app names: {sorted(dupes)}")
    for a in apps:
        missing = set(a.depends_on) - set(names)
        if missing:
            raise InventoryError(f"{a.name} depends on unknown apps {sorted(missing)}")
    return apps


def _validate(a: App) -> None:
    if a.tier not in (1, 2, 3):
        raise InventoryError(f"{a.name}: tier must be 1, 2 or 3")
    if a.kind not in ("service", "batch"):
        raise InventoryError(f"{a.name}: kind must be service or batch")
    if a.kind == "service" and a.port is None:
        raise InventoryError(f"{a.name}: services need a port")
    if a.kind == "batch" and not a.schedule:
        raise InventoryError(f"{a.name}: batch apps need a cron schedule")
    if a.expose and a.kind != "service":
        raise InventoryError(f"{a.name}: only services can be exposed")
