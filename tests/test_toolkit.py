from pathlib import Path

import pytest
import yaml

from ocmigrate import checks, cli, inventory, render, runbook, waves
from ocmigrate.inventory import App, Storage

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "inventory.yml"


def app(name="svc", **kw) -> App:
    base = {"image": "registry/x:1.0.0", "tier": 2, "rto_minutes": 60, "rpo_minutes": 60, "port": 8080}
    return App(name=name, **{**base, **kw})


def codes(apps):
    return {(f.app, f.code) for f in checks.check(apps)}


# -- inventory -----------------------------------------------------------------

def test_example_inventory_loads():
    apps = inventory.load(EXAMPLE)
    assert {a.name for a in apps} >= {"payments-api", "ledger-db", "eod-settlement-batch"}
    assert next(a for a in apps if a.name == "ledger-db").storage.replicated


@pytest.mark.parametrize("yml,msg", [
    ("apps: [{name: a, image: i:1, tier: 2, rto_minutes: 1, rpo_minutes: 1, port: 1, depends_on: [b]}]", "unknown"),
    ("apps: [{name: a, image: i:1, tier: 2, rto_minutes: 1, rpo_minutes: 1, kind: batch}]", "cron schedule"),
    ("apps: [{name: a, image: i:1, tier: 4, rto_minutes: 1, rpo_minutes: 1, port: 1}]", "tier"),
    ("apps: [{name: a, image: i:1, tier: 2, rto_minutes: 1, rpo_minutes: 1, port: 1},"
     " {name: a, image: i:1, tier: 2, rto_minutes: 1, rpo_minutes: 1, port: 1}]", "duplicate"),
    ("apps: [{name: a, image: i:1, tier: 2, rto_minutes: 1, rpo_minutes: 1, port: 1, colour: red}]", "unexpected"),
])
def test_inventory_validation(tmp_path, yml, msg):
    f = tmp_path / "inv.yml"
    f.write_text(yml)
    with pytest.raises(inventory.InventoryError, match=msg):
        inventory.load(f)


# -- render --------------------------------------------------------------------

def kinds(docs):
    return {d["kind"]: d for d in docs}


def all_containers(docs):
    for d in docs:
        spec = d.get("spec", {})
        tpl = spec.get("template") or spec.get("jobTemplate", {}).get("spec", {}).get("template")
        if tpl:
            yield from tpl["spec"]["containers"]


@pytest.mark.parametrize("site", ["primary", "dr"])
def test_every_container_fits_restricted_scc(site):
    apps = inventory.load(EXAMPLE)
    for docs in render.render_all(apps, "ns", site).values():
        for c in all_containers(docs):
            sc = c["securityContext"]
            assert sc["runAsNonRoot"] and not sc["allowPrivilegeEscalation"]
            assert sc["capabilities"]["drop"] == ["ALL"] and "runAsUser" not in sc
            assert "memory" in c["resources"]["limits"]


def test_tier1_service_is_zone_spread_with_pdb():
    a = app(tier=1, rto_minutes=15, rpo_minutes=5, replicas=3, expose=True)
    k = kinds(render.render_app(a, [a], "ns"))
    spread = k["Deployment"]["spec"]["template"]["spec"]["topologySpreadConstraints"][0]
    assert spread["whenUnsatisfiable"] == "DoNotSchedule"
    assert k["PodDisruptionBudget"]["spec"]["minAvailable"] == 2
    assert k["HorizontalPodAutoscaler"]["spec"]["minReplicas"] == 3
    assert k["Route"]["spec"]["tls"]["termination"] == "edge"
    assert k["Deployment"]["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0


@pytest.mark.parametrize("tier,expected", [(1, 3), (2, 1), (3, 0)])
def test_dr_site_replicas_follow_tier_mode(tier, expected):
    a = app(tier=tier, replicas=3)
    k = kinds(render.render_app(a, [a], "ns", site="dr"))
    assert k["Deployment"]["spec"]["replicas"] == expected
    if expected == 0:
        assert "PodDisruptionBudget" not in k and "HorizontalPodAutoscaler" not in k


def test_batch_runs_only_at_primary():
    a = app(kind="batch", schedule="0 2 * * *", port=None)
    primary = kinds(render.render_app(a, [a], "ns"))["CronJob"]["spec"]
    dr = kinds(render.render_app(a, [a], "ns", site="dr"))["CronJob"]["spec"]
    assert primary["suspend"] is False and dr["suspend"] is True
    assert primary["concurrencyPolicy"] == "Forbid"


def test_stateful_uses_recreate_and_no_hpa():
    a = app(storage=Storage("10Gi", "rbd", 5, True), replicas=1)
    k = kinds(render.render_app(a, [a], "ns"))
    assert k["Deployment"]["spec"]["strategy"] == {"type": "Recreate"}
    assert k["PersistentVolumeClaim"]["spec"]["storageClassName"] == "rbd"
    assert "HorizontalPodAutoscaler" not in k


def test_network_policy_admits_only_dependents_and_router():
    db = app("db")
    api = app("api", depends_on=("db",), expose=True)
    other = app("other")
    db_np = kinds(render.render_app(db, [db, api, other], "ns"))["NetworkPolicy"]
    sources = db_np["spec"]["ingress"][0]["from"]
    assert sources == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "api"}}}]
    api_np = kinds(render.render_app(api, [db, api, other], "ns"))["NetworkPolicy"]
    assert any("namespaceSelector" in s for s in api_np["spec"]["ingress"][0]["from"])
    other_np = kinds(render.render_app(other, [db, api, other], "ns"))["NetworkPolicy"]
    assert other_np["spec"]["ingress"] == []  # nobody may call it


# -- checks --------------------------------------------------------------------

def test_clean_app_has_no_findings():
    assert checks.check([app()]) == []


@pytest.mark.parametrize("kw,code", [
    ({"tier": 1, "rto_minutes": 30, "rpo_minutes": 5, "replicas": 3}, "RTO_TIER"),
    ({"tier": 1, "rto_minutes": 15, "rpo_minutes": 5, "replicas": 2}, "REPLICAS"),
    ({"storage": Storage("1Gi", "x")}, "NO_BACKUP"),
    ({"storage": Storage("1Gi", "x", backup_interval_minutes=240)}, "RPO_UNACHIEVABLE"),
    ({"tier": 1, "rto_minutes": 15, "rpo_minutes": 5, "replicas": 1,
      "storage": Storage("1Gi", "x", backup_interval_minutes=5)}, "TIER1_UNREPLICATED"),
    ({"image": "registry/x:latest"}, "MUTABLE_IMAGE"),
    ({"image": "registry/x"}, "MUTABLE_IMAGE"),
    ({"cutover_minutes": 90}, "CUTOVER_EXCEEDS_RTO"),
    ({"env": {"DB_PASSWORD": "hunter2"}}, "LITERAL_SECRET"),
])
def test_check_codes(kw, code):
    assert ("svc", code) in codes([app(**kw)])


def test_digest_pinned_image_is_fine():
    assert codes([app(image="registry/x@sha256:" + "a" * 64)]) == set()


def test_criticality_inversion():
    crit = app("crit", tier=1, rto_minutes=15, rpo_minutes=5, replicas=3, depends_on=("helper",))
    helper = app("helper", tier=3, rto_minutes=480, rpo_minutes=1440, replicas=1)
    assert ("crit", "CRITICALITY_INVERSION") in codes([crit, helper])


def test_example_inventory_is_blocked_for_the_documented_reasons():
    found = codes(inventory.load(EXAMPLE))
    assert ("payments-api", "CRITICALITY_INVERSION") in found
    assert ("notification-svc", "LITERAL_SECRET") in found
    assert ("ledger-db", "REPLICAS") not in found  # resilience comes from replicated storage


# -- waves & runbook -----------------------------------------------------------

def test_waves_put_dependencies_first_and_low_tiers_first():
    apps = inventory.load(EXAMPLE)
    plan = waves.plan_waves(apps)
    position = {a.name: i for i, w in enumerate(plan) for a in w}
    for a in apps:
        for d in a.depends_on:
            assert position[d] < position[a.name]
    assert [a.tier for a in plan[0]] == sorted((a.tier for a in plan[0]), reverse=True)


def test_wave_size_cap():
    apps = [app(f"a{i}") for i in range(5)]
    assert [len(w) for w in waves.plan_waves(apps, max_per_wave=2)] == [2, 2, 1]


def test_cycle_is_reported():
    with pytest.raises(waves.CycleError, match="a -> b -> a|b -> a -> b"):
        waves.plan_waves([app("a", depends_on=("b",)), app("b", depends_on=("a",))])


def test_runbook_covers_state_and_batch_safety():
    md = runbook.render(waves.plan_waves(inventory.load(EXAMPLE)), "payments")
    assert "## Wave 1" in md and "### payments-api" in md
    assert "final snapshot" in md and "replication lag" in md
    assert "never runs twice" in md and "Rollback" in md


# -- cli -----------------------------------------------------------------------

def test_cli_check_exit_code(capsys):
    assert cli.main(["check", str(EXAMPLE)]) == 1
    assert "BLOCKING" in capsys.readouterr().out


def test_cli_render_writes_plain_yaml(tmp_path):
    assert cli.main(["render", str(EXAMPLE), "--namespace", "payments", "--out", str(tmp_path)]) == 0
    text = (tmp_path / "primary" / "payments-api.yaml").read_text()
    assert "&id" not in text and "*id" not in text
    docs = list(yaml.safe_load_all(text))
    assert {d["kind"] for d in docs} >= {"Deployment", "Service", "Route", "PodDisruptionBudget", "NetworkPolicy"}
