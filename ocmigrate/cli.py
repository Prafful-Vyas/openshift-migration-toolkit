"""`ocmigrate` CLI: check, render, waves, runbook."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import checks, inventory, render, runbook, waves


class _NoAliases(yaml.SafeDumper):
    """Write shared sub-dicts (selectors, probes) out in full instead of as &id001
    anchors. Anchors are valid YAML, but they make generated manifests hard to review."""

    def ignore_aliases(self, data):
        return True


def _dump(docs: list[dict]) -> str:
    return "---\n".join(yaml.dump(d, Dumper=_NoAliases, sort_keys=False) for d in docs)


def cmd_check(args) -> int:
    findings = checks.check(inventory.load(args.inventory))
    for f in sorted(findings, key=lambda f: (f.severity != "ERROR", f.app, f.code)):
        print(f"{f.severity:5} {f.app:24} {f.code:22} {f.message}")
    blocking = checks.blocking(findings)
    print(f"\n{len(findings)} findings, {'BLOCKING' if blocking else 'no blockers'}")
    return 1 if blocking else 0


def cmd_render(args) -> int:
    apps = inventory.load(args.inventory)
    out = Path(args.out) / args.site
    out.mkdir(parents=True, exist_ok=True)
    (out / "00-default-deny.yaml").write_text(_dump([render.default_deny(args.namespace)]), encoding="utf-8")
    for name, docs in render.render_all(apps, args.namespace, args.site).items():
        (out / f"{name}.yaml").write_text(_dump(docs), encoding="utf-8")
    print(f"wrote {len(apps) + 1} files to {out}")
    return 0


def cmd_waves(args) -> int:
    plan = waves.plan_waves(inventory.load(args.inventory), args.max_per_wave)
    doc = [{"wave": i, "apps": [{"name": a.name, "kind": a.kind, "tier": a.tier} for a in w]}
           for i, w in enumerate(plan, 1)]
    text = json.dumps(doc, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_runbook(args) -> int:
    plan = waves.plan_waves(inventory.load(args.inventory), args.max_per_wave)
    Path(args.out).write_text(runbook.render(plan, args.namespace), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ocmigrate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="readiness checks; exit 1 on blocking findings")
    p.add_argument("inventory")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("render", help="write OpenShift manifests")
    p.add_argument("inventory")
    p.add_argument("--namespace", required=True)
    p.add_argument("--site", choices=["primary", "dr"], default="primary")
    p.add_argument("--out", default="out")
    p.set_defaults(fn=cmd_render)

    for name, fn in [("waves", cmd_waves), ("runbook", cmd_runbook)]:
        p = sub.add_parser(name)
        p.add_argument("inventory")
        p.add_argument("--max-per-wave", type=int)
        p.add_argument("--namespace", default="migrated")
        p.add_argument("--out", default=None if name == "waves" else "RUNBOOK.md")
        p.set_defaults(fn=fn)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (inventory.InventoryError, waves.CycleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
