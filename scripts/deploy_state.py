#!/usr/bin/env python3
"""Back up live release state and safely adopt matching Switchyard policies."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


MANAGED_BY = "app.kubernetes.io/managed-by"
RELEASE = "meta.helm.sh/release-name"
NAMESPACE = "meta.helm.sh/release-namespace"
POLICIES = {
    ("networking.k8s.io/v1", "NetworkPolicy"): "-consumers",
    ("cilium.io/v2", "CiliumNetworkPolicy"): "-nodeport-ingress",
}


def command(*args: str, input: str | None = None) -> str:
    return subprocess.run(
        args, input=input, text=True, stdout=subprocess.PIPE, check=True
    ).stdout


def manifest_objects(manifest: str) -> list[dict]:
    # kubectl handles YAML without adding a Python YAML dependency. Multiple
    # documents are printed as consecutive JSON objects, not a JSON array.
    raw = command(
        "kubectl", "create", "--dry-run=client", "--validate=false",
        "-f", "-", "-o", "json", input=manifest,
    )
    decoder = json.JSONDecoder()
    objects = []
    while raw.strip():
        item, end = decoder.raw_decode(raw.lstrip())
        raw = raw.lstrip()[end:]
        objects.extend(item["items"] if item.get("kind") == "List" else [item])
    return objects


def resource(obj: dict) -> str:
    group = obj["apiVersion"].split("/")[0] if "/" in obj["apiVersion"] else ""
    return obj["kind"].lower() + ("." + group if group else "")


def identity(obj: dict) -> tuple[str, str]:
    return resource(obj), obj["metadata"]["name"]


def ownership_patch(live: dict, desired: dict, release: str, namespace: str) -> dict | None:
    metadata = live["metadata"]
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    expected = {RELEASE: release, NAMESPACE: namespace}
    if labels.get(MANAGED_BY) == "Helm" and all(
        annotations.get(key) == value for key, value in expected.items()
    ):
        return None
    label = "/".join(identity(live))
    suffix = POLICIES.get((live["apiVersion"], live["kind"]))
    if suffix is None or metadata["name"] != release + suffix:
        raise ValueError(f"{label}: existing resource is not owned by this Helm release")
    if metadata.get("namespace") != namespace or metadata.get("ownerReferences"):
        raise ValueError(f"{label}: refusing adoption of a policy with another owner")
    if labels.get(MANAGED_BY) not in (None, "Helm") or any(
        key in annotations and annotations[key] != value for key, value in expected.items()
    ):
        raise ValueError(f"{label}: conflicting Helm ownership; refusing adoption")
    for key in ("app.kubernetes.io/name", "app.kubernetes.io/instance"):
        if not desired["metadata"]["labels"].get(key) or labels.get(key) != desired["metadata"]["labels"][key]:
            raise ValueError(f"{label}: policy labels do not identify this release")
    # Comparing the complete policy body also catches Cilium's optional `specs`.
    body = lambda obj: {k: v for k, v in obj.items() if k not in ("metadata", "status")}
    if body(live) != body(desired):
        raise ValueError(f"{label}: live policy differs from chart; reconcile access rules before adoption")
    return {"metadata": {
        # A concurrent edit must fail rather than be silently adopted.
        "resourceVersion": metadata["resourceVersion"],
        "labels": {MANAGED_BY: "Helm"},
        "annotations": expected,
    }}


def restorable(obj: dict) -> dict:
    obj = deepcopy(obj)
    obj.pop("status", None)
    metadata = obj["metadata"]
    for key in ("uid", "resourceVersion", "generation", "creationTimestamp", "managedFields"):
        metadata.pop(key, None)
    metadata.get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


def nodeport_only(objects: list[dict]) -> bool:
    return any(
        obj["kind"] == "Service"
        and obj["spec"].get("type") == "LoadBalancer"
        and obj["spec"].get("loadBalancerClass") == "daedalus.ai/nodeport-only"
        for obj in objects
    )


def prepare(args: argparse.Namespace) -> None:
    desired = manifest_objects(args.manifest.read_text())
    releases = json.loads(command(
        "helm", "list", "--namespace", args.namespace, "--all",
        "--filter", "^" + args.release + "$", "-o", "json",
    ))
    if releases and releases[0]["status"].startswith("pending-"):
        raise ValueError("Helm release has an operation in progress")
    previous = command(
        "helm", "get", "manifest", args.release, "--namespace", args.namespace,
    ) if releases else ""
    desired_by_id = {identity(obj): obj for obj in desired}
    inventory = {identity(obj): obj for obj in manifest_objects(previous)} if previous else {}
    inventory.update(desired_by_id)
    live_objects = []
    patches = []
    for key, obj in inventory.items():
        if obj["metadata"].get("namespace") != args.namespace:
            raise ValueError(f"{key}: snapshot only supports this release's namespaced resources")
        raw = command(
            "kubectl", "--namespace", args.namespace, "get", *key,
            "--ignore-not-found", "-o", "json",
        )
        if not raw.strip():
            continue
        live = json.loads(raw)
        if key in desired_by_id:
            patch = ownership_patch(live, desired_by_id[key], args.release, args.namespace)
            if patch:
                patches.append((live, patch))
        live_objects.append(live)

    # Finish every ownership check and backup before the first cluster mutation.
    args.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(
        prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + args.release + "-",
        dir=args.backup_root,
    )).resolve()
    (backup / "live.json").write_text(json.dumps(live_objects, indent=2) + "\n")
    (backup / "previous-manifest.yaml").write_text(previous)
    (backup / "desired-manifest.yaml").write_text(args.manifest.read_text())
    secret = command(
        "kubectl", "--namespace", args.namespace, "get", "secret", args.secret,
        "--ignore-not-found", "-o", "json",
    )
    if secret.strip():
        (backup / "secret.json").write_text(json.dumps(restorable(json.loads(secret)), indent=2) + "\n")

    chart = backup / "recovery-chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: switchyard-recovery\nversion: 0.1.0\n")
    for index, obj in enumerate(live_objects):
        # JSON is valid YAML; escape template delimiters in arbitrary ConfigMap text.
        data = json.dumps(restorable(obj), indent=2).replace("{{", r"\u007b\u007b")
        (chart / "templates" / f"{index}.yaml").write_text(data + "\n")

    q = shlex.quote
    common = f"{q(args.release)} {q(str(chart))} --namespace {q(args.namespace)}"
    restore = ["#!/usr/bin/env bash", "set -euo pipefail",
               f'[[ "$(kubectl config current-context)" == {q(args.context)} ]] || {{ echo "wrong Kubernetes context" >&2; exit 1; }}']
    if secret.strip():
        restore.append(f"kubectl --namespace {q(args.namespace)} apply -f {q(str(backup / 'secret.json'))}")
    if live_objects:
        # Do not roll back to stale Helm networking. This chart records the live
        # Service, policies and application, including pre-existing adopted rules.
        restore.extend([
            f"helm upgrade --install {common} --timeout {q(args.timeout)} --history-max 10",
            f"kubectl --namespace {q(args.namespace)} rollout status deployment/{q(args.release)} --timeout={q(args.timeout)}",
        ])
    else:
        restore.append('echo "No previous release resources to restore; inspect the failed initial install." >&2')
        restore.append("exit 1")
    (backup / "restore.sh").write_text("\n".join(restore) + "\n")
    (backup / "restore.sh").chmod(0o700)
    args.state.write_text(json.dumps({
        "backup": str(backup), "nodeport_only": nodeport_only(desired),
        "can_restore": bool(live_objects),
    }))
    print(f"Deployment backup: {backup}", flush=True)
    for live, patch in patches:
        command(
            "kubectl", "--namespace", args.namespace, "patch", *identity(live),
            "--type=merge", "-p", json.dumps(patch),
        )
        print(f"Repaired Helm ownership: {live['kind']}/{live['metadata']['name']}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--timeout", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        prepare(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Deployment preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
