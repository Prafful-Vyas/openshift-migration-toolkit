"""Render OpenShift manifests from the inventory.

Output is plain Kubernetes objects plus `route.openshift.io/v1` Routes, written to be
admitted under OpenShift's default `restricted-v2` SCC: no fixed UID (OpenShift
assigns one from the namespace range), non-root, no privilege escalation, all
capabilities dropped, RuntimeDefault seccomp.
"""

from __future__ import annotations

from .inventory import App
from .policy import POLICY, dr_replicas

LABEL = "app.kubernetes.io/name"


def _meta(app: App, namespace: str, suffix: str = "") -> dict:
    return {
        "name": app.name + suffix,
        "namespace": namespace,
        "labels": {LABEL: app.name, "bcp.example.com/tier": str(app.tier),
                   "app.kubernetes.io/managed-by": "ocmigrate"},
        "annotations": {"bcp.example.com/rto-minutes": str(app.rto_minutes),
                        "bcp.example.com/rpo-minutes": str(app.rpo_minutes)},
    }


def _container(app: App) -> dict:
    c = {
        "name": app.name,
        "image": app.image,
        "env": [{"name": k, "value": v} for k, v in sorted(app.env.items())],
        "envFrom": [{"secretRef": {"name": s}} for s in app.secrets],
        "resources": {"requests": {"cpu": app.cpu, "memory": app.memory},
                      "limits": {"memory": app.memory}},  # no CPU limit: avoids CFS throttling
        "securityContext": {
            "runAsNonRoot": True, "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
    if app.kind == "service":
        c["ports"] = [{"name": "http", "containerPort": app.port}]
        probe = {"httpGet": {"path": app.health_path, "port": "http"}, "periodSeconds": 10}
        c["readinessProbe"] = {**probe, "failureThreshold": 3}
        c["livenessProbe"] = {**probe, "failureThreshold": 6, "initialDelaySeconds": 20}
        c["startupProbe"] = {**probe, "failureThreshold": 30}
    if app.storage:
        c["volumeMounts"] = [{"name": "data", "mountPath": "/var/lib/app"}]
    return c


def _pod_spec(app: App) -> dict:
    spec = {"containers": [_container(app)], "terminationGracePeriodSeconds": 45}
    if app.storage:
        spec["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": f"{app.name}-data"}}]
    return spec


def render_app(app: App, apps: list[App], namespace: str, site: str = "primary") -> list[dict]:
    pol = POLICY[app.tier]
    replicas = app.replicas if site == "primary" else dr_replicas(app.tier, app.replicas)
    selector = {LABEL: app.name}
    out: list[dict] = []

    if app.storage:
        out.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": _meta(app, namespace, "-data"),
                    "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": app.storage.storage_class,
                             "resources": {"requests": {"storage": app.storage.size}}}})

    if app.kind == "batch":
        out.append({
            "apiVersion": "batch/v1", "kind": "CronJob", "metadata": _meta(app, namespace),
            "spec": {
                "schedule": app.schedule,
                "concurrencyPolicy": "Forbid",
                # Only one site may run a batch job; the DR copy stays suspended until
                # failover, otherwise both sites would post the same batch twice.
                "suspend": site != "primary",
                "startingDeadlineSeconds": 600,
                "jobTemplate": {"spec": {"backoffLimit": 2, "template": {
                    "metadata": {"labels": selector},
                    "spec": {**_pod_spec(app), "restartPolicy": "Never"}}}},
            },
        })
        return out

    pod_spec = _pod_spec(app)
    if pol.zone_spread:
        pod_spec["topologySpreadConstraints"] = [{
            "maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone",
            "whenUnsatisfiable": pol.zone_spread, "labelSelector": {"matchLabels": selector}}]
        pod_spec["affinity"] = {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [{
            "weight": 100, "podAffinityTerm": {"topologyKey": "kubernetes.io/hostname",
                                               "labelSelector": {"matchLabels": selector}}}]}}
    out.append({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": _meta(app, namespace),
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector},
            # Stateful RWO volumes cannot attach to two pods during a rolling update.
            "strategy": ({"type": "Recreate"} if app.storage else
                         {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}),
            "template": {"metadata": {"labels": selector}, "spec": pod_spec},
        },
    })
    out.append({"apiVersion": "v1", "kind": "Service", "metadata": _meta(app, namespace),
                "spec": {"selector": selector, "ports": [{"name": "http", "port": 80, "targetPort": "http"}]}})
    if app.expose:
        out.append({"apiVersion": "route.openshift.io/v1", "kind": "Route", "metadata": _meta(app, namespace),
                    "spec": {"to": {"kind": "Service", "name": app.name}, "port": {"targetPort": "http"},
                             "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}}})
    if pol.pdb_min_available and replicas > 0:
        out.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": _meta(app, namespace),
                    "spec": {"minAvailable": min(pol.pdb_min_available, replicas), "selector": {"matchLabels": selector}}})
    if pol.hpa_max_replicas and replicas > 0 and not app.storage:
        out.append({"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": _meta(app, namespace),
                    "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": app.name},
                             "minReplicas": replicas, "maxReplicas": max(replicas, pol.hpa_max_replicas),
                             "metrics": [{"type": "Resource", "resource": {
                                 "name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}]}})
    out.append(_network_policy(app, apps, namespace))
    return out


def _network_policy(app: App, apps: list[App], namespace: str) -> dict:
    """Allow ingress only from apps that declare a dependency on this one (plus the
    OpenShift router for exposed services). Everything else is denied by the
    namespace-wide default-deny policy."""
    sources = [{"podSelector": {"matchLabels": {LABEL: a.name}}}
               for a in apps if app.name in a.depends_on]
    if app.expose:
        sources.append({"namespaceSelector": {"matchLabels": {
            "policy-group.network.openshift.io/ingress": ""}}})
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": _meta(app, namespace, "-ingress"),
            "spec": {"podSelector": {"matchLabels": {LABEL: app.name}}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": sources, "ports": [{"port": app.port}]}] if sources else []}}


def default_deny(namespace: str) -> dict:
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "default-deny-ingress", "namespace": namespace},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"]}}


def render_all(apps: list[App], namespace: str, site: str = "primary") -> dict[str, list[dict]]:
    return {a.name: render_app(a, apps, namespace, site) for a in apps}
