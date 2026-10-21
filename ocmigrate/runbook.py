"""Cutover runbook generation (Markdown), one section per wave."""

from __future__ import annotations

from .inventory import App
from .policy import POLICY


def _app_steps(a: App, ns: str) -> list[str]:
    steps = []
    if a.storage:
        if a.storage.replicated:
            steps.append(f"Confirm DR replication lag for `{a.name}-data` is under RPO ({a.rpo_minutes}m).")
        steps.append(f"Freeze writes to `{a.name}` at source and take a final snapshot (RPO {a.rpo_minutes}m).")
        steps.append(f"Restore the snapshot into PVC `{a.name}-data` and verify row counts / checksums against source.")
    steps.append(f"`oc apply -n {ns} -f out/primary/{a.name}.yaml`")
    if a.kind == "batch":
        steps.append(f"Suspend the legacy scheduler entry for `{a.name}` before the first OpenShift run "
                     f"(`{a.schedule}`), so the batch never runs twice.")
        steps.append(f"`oc create job -n {ns} {a.name}-smoke --from=cronjob/{a.name}` and check it completes.")
    else:
        steps.append(f"`oc rollout status -n {ns} deploy/{a.name} --timeout=10m`")
        path = a.health_path.lstrip("/")
        if a.expose:
            steps.append(f"Smoke test through the router: `curl -fsS https://$(oc get route -n {ns} {a.name} "
                         f"-o jsonpath='{{.spec.host}}')/{path}` returns 200.")
            steps.append("Shift traffic (GSLB/DNS weight 10% → 50% → 100%), watching error rate and p99 latency.")
        else:
            steps.append(f"Smoke test in-cluster: `oc run smoke-{a.name} -n {ns} --rm -i --restart=Never "
                         f"--image=registry.access.redhat.com/ubi9/ubi-minimal -- curl -fsS http://{a.name}/{path}`.")
            steps.append("Repoint legacy consumers at the OpenShift service, one consumer at a time.")
    return steps


def _rollback(a: App, ns: str) -> str:
    if a.kind == "batch":
        return f"`oc patch -n {ns} cronjob/{a.name} -p '{{\"spec\":{{\"suspend\":true}}}}'` and re-enable the legacy schedule."
    return (f"Shift traffic back to legacy (weight 100%), then `oc scale -n {ns} deploy/{a.name} --replicas=0`."
            + (" Reconcile any writes made on OpenShift since cutover." if a.storage else ""))


def render(waves: list[list[App]], namespace: str) -> str:
    lines = ["# OpenShift migration cutover runbook", "",
             f"Namespace: `{namespace}` · {sum(len(w) for w in waves)} apps in {len(waves)} waves", ""]
    for i, wave in enumerate(waves, 1):
        budget = sum(a.cutover_minutes for a in wave)
        lines += [f"## Wave {i} — {', '.join(a.name for a in wave)}", "",
                  f"Estimated hands-on time: **{budget} min** (apps cut over sequentially).", "",
                  "| App | Tier | RTO | RPO | DR mode |", "|---|---|---|---|---|"]
        lines += [f"| {a.name} | {a.tier} | {a.rto_minutes}m | {a.rpo_minutes}m | {POLICY[a.tier].dr_mode} |" for a in wave]
        lines.append("")
        for a in wave:
            lines += [f"### {a.name}", "", "**Pre-checks**", "",
                      f"- Dependencies already live on OpenShift: {', '.join(a.depends_on) or 'none'}.",
                      f"- Secrets present: {', '.join(f'`{s}`' for s in a.secrets) or 'none required'}.",
                      "", "**Cutover**", ""]
            lines += [f"{n}. {s}" for n, s in enumerate(_app_steps(a, namespace), 1)]
            lines += ["", f"**Rollback:** {_rollback(a, namespace)}", ""]
    return "\n".join(lines)
