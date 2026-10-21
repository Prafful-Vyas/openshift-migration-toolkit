# OpenShift Migration Toolkit (BCP-aware)

A Python CLI for moving an application estate from VMs to **Red Hat OpenShift**
without losing business continuity. You describe each workload once, with its
criticality tier, **RTO/RPO**, dependencies, storage and backups. The toolkit then:

1. **checks** whether the workload *can* meet its continuity targets once migrated,
   and blocks the wave if it cannot;
2. **renders** hardened OpenShift manifests for the **primary and DR sites**;
3. **plans migration waves** from the dependency graph;
4. writes a **cutover runbook** with rollback steps; and
5. ships an **Ansible playbook** that applies a wave and verifies each app.

```
inventory.yml ──► ocmigrate check   ──► ERROR/WARN findings (exit 1 blocks the wave)
      │
      ├────────► ocmigrate render --site primary|dr ──► Deployment/CronJob, Service, Route,
      │                                                  PDB, HPA, PVC, NetworkPolicy
      ├────────► ocmigrate waves   ──► waves.json ──► ansible/migrate-wave.yml -e wave=N
      └────────► ocmigrate runbook ──► RUNBOOK.md (pre-checks, cutover, rollback per app)
```

## Continuity policy (`ocmigrate/policy.py`)

| Tier | Max RTO | Max RPO | Min replicas | PDB minAvailable | Zone spread | DR site |
|---|---|---|---|---|---|---|
| 1 | 15m | 5m | 3 | 2 | `DoNotSchedule` | active-active (same replicas) |
| 2 | 60m | 60m | 2 | 1 | `ScheduleAnyway` | warm standby (1 replica) |
| 3 | 8h | 24h | 1 | none | none | cold (scaled to 0) |

## What the renderer gets right

* **`restricted-v2` SCC compatible**: non-root, no fixed UID (OpenShift assigns one
  from the namespace range), `allowPrivilegeEscalation: false`, all capabilities
  dropped, `RuntimeDefault` seccomp.
* **Availability**: readiness, liveness and startup probes; zero-unavailable rolling
  updates; zone topology spread plus host anti-affinity; PodDisruptionBudgets so node
  drains during OpenShift upgrades never take a tier 1 service below quorum; HPA
  floors at the declared replica count.
* **State**: PVCs with the declared storage class, and a `Recreate` strategy for
  ReadWriteOnce volumes, which cannot attach to two pods during a rolling update.
* **Batch safety**: CronJobs use `concurrencyPolicy: Forbid`, and the **DR copy is
  `suspend: true`**, so an end-of-day batch never posts twice from both sites.
* **Network**: a namespace default-deny policy, plus per-app policies that admit
  ingress only from apps that declare a dependency (and from the router for
  exposed services). Routes use edge TLS with an HTTP → HTTPS redirect.

## Readiness checks (`ocmigrate/checks.py`)

| Code | Severity | Catches |
|---|---|---|
| `RTO_TIER` / `RPO_TIER` | ERROR | Targets looser than the tier allows |
| `REPLICAS` | ERROR | Stateless service under the tier's minimum replicas |
| `NO_BACKUP` | ERROR | Stateful app with neither replication nor backups |
| `RPO_UNACHIEVABLE` | ERROR | Backup interval longer than the RPO, with no replication |
| `TIER1_UNREPLICATED` | ERROR | Tier 1 data that could only be restored from backup |
| `CRITICALITY_INVERSION` | ERROR | Critical app depending on a less critical one (its real RTO is the dependency's) |
| `LITERAL_SECRET` | ERROR | Credentials in plain env vars |
| `MUTABLE_IMAGE` | WARN | `:latest` or untagged images |
| `CUTOVER_EXCEEDS_RTO` | WARN | Stop-and-move cutover longer than the RTO (run blue/green instead) |
| `RWO_REPLICAS` | WARN | More than one replica on a ReadWriteOnce volume |

The example estate (`examples/inventory.yml`, a payments slice) intentionally fails
with five findings. For example, `payments-api` (tier 1) depends on a tier 3
notification service, so its real RTO is 240 minutes, not 15.

## Usage

```bash
pip install -e .[dev]
ocmigrate check   examples/inventory.yml
ocmigrate render  examples/inventory.yml --namespace payments --out out
ocmigrate render  examples/inventory.yml --namespace payments --site dr --out out
ocmigrate waves   examples/inventory.yml --out out/waves.json
ocmigrate runbook examples/inventory.yml --namespace payments --out out/RUNBOOK.md
ansible-playbook ansible/migrate-wave.yml -e wave=1 -e namespace=payments   # needs oc login
```

## What was verified

* `pytest`: 34 tests. They cover inventory validation, SCC compliance of every
  rendered container at both sites, tier 1 spread/PDB/HPA/Route, DR replica modes,
  batch suspension at DR, stateful strategy, NetworkPolicy sources, every check code,
  wave ordering, cycle detection, wave size caps, runbook content and CLI exit codes.
* All 44 non-Route manifests rendered from the example estate (primary + DR) pass
  `kubeconform -strict` against the Kubernetes 1.29 schemas. CI runs the same check
  and `ansible-lint`.
* **Not run against a live OpenShift cluster.** The Ansible playbook has not been run
  against a real API server, and the Route objects are checked by unit tests only,
  because no public JSON schema exists for them.
