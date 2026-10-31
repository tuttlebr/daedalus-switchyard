"""Deployment ownership, live-state recovery and NodePort readiness regressions."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploy_state", ROOT / "scripts/deploy_state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def policy():
    return {
        "apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy",
        "metadata": {
            "name": "switchyard-nodeport-ingress", "namespace": "daedalus",
            "labels": {
                "app.kubernetes.io/name": "switchyard",
                "app.kubernetes.io/instance": "switchyard",
                state.MANAGED_BY: "Helm",
            },
        },
        "spec": {
            "endpointSelector": {"matchLabels": {"app.kubernetes.io/instance": "switchyard"}},
            "ingress": [{"fromCIDR": ["192.168.1.0/24"]}],
        },
    }


class Ownership(unittest.TestCase):
    def setUp(self):
        self.desired = policy()
        self.live = deepcopy(self.desired)
        self.live["metadata"]["resourceVersion"] = "123"
        self.live["status"] = {"conditions": [{"type": "Valid", "status": "True"}]}

    def adoption(self):
        return state.ownership_patch(self.live, self.desired, "switchyard", "daedalus")

    def test_repairs_only_metadata_with_concurrency_guard(self):
        before = deepcopy(self.live)
        result = self.adoption()
        self.assertEqual(set(result), {"metadata"})
        self.assertEqual(result["metadata"]["resourceVersion"], "123")
        self.assertEqual(result["metadata"]["annotations"], {
            state.RELEASE: "switchyard", state.NAMESPACE: "daedalus",
        })
        self.assertEqual(before, self.live)

    def test_already_owned_policy_is_not_patched(self):
        self.live["metadata"]["annotations"] = {
            state.RELEASE: "switchyard", state.NAMESPACE: "daedalus",
        }
        self.assertIsNone(self.adoption())

    def test_rejects_conflicting_owners_labels_and_network_rules(self):
        original = deepcopy(self.live)
        changes = [
            ("annotations", {state.RELEASE: "another-release"}),
            ("annotations", {state.NAMESPACE: "another-namespace"}),
            ("ownerReferences", [{"uid": "another-controller"}]),
            ("namespace", "other"),
            ("name", "unrelated-policy"),
            ("labels", {**original["metadata"]["labels"], state.MANAGED_BY: "Terraform"}),
            ("labels", {**original["metadata"]["labels"], "app.kubernetes.io/instance": "other"}),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.live = deepcopy(original)
                self.live["metadata"][key] = value
                with self.assertRaises(ValueError):
                    self.adoption()
        for key, value in (("spec", {"ingress": []}), ("specs", [{"ingress": []}])):
            with self.subTest(key=key):
                self.live = deepcopy(original)
                self.live[key] = value
                with self.assertRaises(ValueError):
                    self.adoption()

    def test_does_not_adopt_arbitrary_resources(self):
        self.live["kind"] = "ConfigMap"
        with self.assertRaises(ValueError):
            self.adoption()

    def test_native_networkpolicy_is_supported(self):
        for obj in (self.live, self.desired):
            obj.update(apiVersion="networking.k8s.io/v1", kind="NetworkPolicy")
            obj["metadata"]["name"] = "switchyard-consumers"
        self.assertIsNotNone(self.adoption())

    def test_restore_keeps_live_network_and_removes_server_metadata(self):
        obj = deepcopy(self.live)
        obj["metadata"].update(uid="old", generation=2, managedFields=[])
        obj["metadata"]["annotations"] = {"kubectl.kubernetes.io/last-applied-configuration": "old"}
        restored = state.restorable(obj)
        self.assertEqual(restored["spec"], obj["spec"])
        self.assertNotIn("status", restored)
        self.assertNotIn("resourceVersion", restored["metadata"])
        self.assertNotIn("uid", restored["metadata"])
        self.assertEqual(obj["metadata"]["resourceVersion"], "123")


class Preparation(unittest.TestCase):
    def test_missing_source_filter_stops_before_release_or_secret_mutations(self):
        desired = policy()
        desired["spec"]["ingress"] = [{"fromEntities": ["world"]}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.yaml"
            manifest.write_text("fixture")
            args = state.argparse.Namespace(manifest=manifest)
            for value in ("false", ""):
                with self.subTest(value=value), patch.object(
                    state, "manifest_objects", return_value=[desired]
                ), patch.object(state, "command", return_value=value) as command:
                    with self.assertRaisesRegex(ValueError, "bpf.lbSourceRangeAllTypes"):
                        state.prepare(args)
                    command.assert_called_once_with(
                        "kubectl", "--namespace", "kube-system", "get", "configmap", "cilium-config",
                        "--output=jsonpath={.data.bpf-lb-source-range-all-types}",
                    )

    def test_backup_and_recovery_chart_precede_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.yaml"
            manifest.write_text("fixture")
            args = state.argparse.Namespace(
                manifest=manifest, state=root / "state.json", backup_root=root / "backups",
                namespace="daedalus", release="switchyard", secret="switchyard-api-key",
                context="fixture", timeout="1m",
            )
            service = {
                "apiVersion": "v1", "kind": "Service",
                "metadata": {"name": "switchyard", "namespace": "daedalus",
                             "labels": {state.MANAGED_BY: "Helm"},
                             "annotations": {state.RELEASE: "switchyard", state.NAMESPACE: "daedalus"}},
                "spec": {"type": "LoadBalancer", "loadBalancerClass": "daedalus.ai/nodeport-only",
                         "clusterIP": "10.97.46.34", "ports": [{"port": 4000, "nodePort": 30260}],
                         "loadBalancerSourceRanges": ["192.168.1.0/24"]},
            }
            objects = [policy(), service]
            objects[0]["spec"]["ingress"].append({"fromEntities": ["world"]})
            live = {state.identity(obj): deepcopy(obj) for obj in objects}
            live[state.identity(objects[0])]["metadata"]["resourceVersion"] = "123"
            patches = []

            def run(*argv, **kwargs):
                if "cilium-config" in argv:
                    return "true"
                if argv[:2] == ("helm", "list"):
                    return '[{"status":"deployed"}]'
                if argv[:3] == ("helm", "get", "manifest"):
                    return "previous"
                if "patch" in argv:
                    backup = Path(json.loads(args.state.read_text())["backup"])
                    self.assertTrue((backup / "restore.sh").exists())
                    self.assertTrue((backup / "live.json").exists())
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
                    patches.append(argv)
                    return ""
                if "get" in argv:
                    i = argv.index("get")
                    return json.dumps(live[(argv[i+1], argv[i+2])]) if argv[i+1] != "secret" else ""
                self.fail(argv)

            with patch.object(state, "manifest_objects", return_value=objects), patch.object(state, "command", side_effect=run):
                state.prepare(args)
            self.assertEqual(len(patches), 1)
            info = json.loads(args.state.read_text())
            self.assertTrue(info["nodeport_only"])
            backup = Path(info["backup"])
            restored_service = json.loads((backup / "recovery-chart/templates/1.yaml").read_text())
            self.assertEqual(restored_service["spec"], service["spec"])
            restore = (backup / "restore.sh").read_text()
            self.assertIn("helm upgrade --install", restore)
            self.assertNotIn("helm rollback", restore)
            self.assertNotIn("--wait", restore)

    def test_kubectl_json_stream_and_list(self):
        objects = [policy(), policy()]
        for output in ("\n".join(map(json.dumps, objects)), json.dumps({"kind": "List", "items": objects})):
            with self.subTest(output=output), patch.object(state, "command", return_value=output):
                self.assertEqual(state.manifest_objects("fixture"), objects)


@unittest.skipUnless(shutil.which("helm"), "requires Helm")
class NetworkPolicyChart(unittest.TestCase):
    def render(self, *overrides):
        return subprocess.run(
            ["helm", "template", "switchyard", str(ROOT / "charts/switchyard"),
             "--namespace", "daedalus", "--show-only", "templates/networkpolicy.yaml", *overrides],
            capture_output=True, text=True, timeout=15,
        )

    def test_forwarding_rejects_services_without_an_allow_list(self):
        for overrides in (
            ["--set", "service.type=NodePort"],
            ["--set-json", "service.loadBalancerSourceRanges=[]"],
            ["--set-json", 'service.annotations={"service.cilium.io/src-ranges-policy":"deny"}'],
        ):
            with self.subTest(overrides=overrides):
                result = self.render(*overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("filter LAN clients before SNAT", result.stderr)

    def test_default_and_internal_only_configurations_render(self):
        for overrides in (
            [],
            ["--set", "service.type=ClusterIP"],
            ["--set", "service.type=NodePort", "--set", "networkPolicy.allowNodePortSNAT=false"],
        ):
            with self.subTest(overrides=overrides):
                result = self.render(*overrides)
                self.assertEqual(result.returncode, 0, result.stderr)


class DeployShell(unittest.TestCase):
    def run_deploy(self, *, nodeport=True, failure="", namespace_exists=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "bin").mkdir()
            for name in ("deploy.sh", "list_api_key_envs.py"):
                shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
            (root / "scripts/check_capabilities.py").write_text("pass\n")
            (root / "scripts/deploy_state.py").write_text(
                "import json, pathlib, sys\n"
                "root = pathlib.Path(__file__).parents[1]\n"
                "(root / 'restore.sh').write_text('echo restored >> \"$CALL_LOG\"\\n')\n"
                "path = pathlib.Path(sys.argv[sys.argv.index('--state')+1])\n"
                f"path.write_text(json.dumps({{'backup': str(root), 'nodeport_only': {nodeport!r}, 'can_restore': True}}))\n"
            )
            routes = root / "routes.toml"
            routes.write_text('[llm_clients.fixture]\nformat="openai_chat"\nbase_url="http://fixture"\n')
            cli = """#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if args == ["config", "current-context"]: print("fixture")
if name == "kubectl" and "get" in args and "namespace" in args and os.environ["NS_EXISTS"] == "yes":
    print("namespace/daedalus")
if "endpointslice" in args: print("10.0.2.35")
failure = os.environ["FAILURE"]
if failure == "upgrade" and name == "helm" and args[0] == "upgrade": sys.exit(1)
if failure == "rollout" and "rollout" in args: sys.exit(1)
if "--raw" in args:
    if failure == "health": sys.exit(1)
    print('{"status":"ok"}')
"""
            for name in ("helm", "kubectl"):
                path = root / "bin" / name
                path.write_text(cli)
                path.chmod(0o755)
            calls = root / "calls"
            result = subprocess.run(
                ["bash", str(root / "scripts/deploy.sh")], capture_output=True, text=True,
                env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                     "CALL_LOG": str(calls), "FAILURE": failure,
                     "NS_EXISTS": "yes" if namespace_exists else "no",
                     "ROUTES_FILE": str(routes), "EXPECTED_CONTEXT": "fixture",
                     "IMAGE_REPOSITORY": "example.test/fixture"},
                timeout=15,
            )
            return result, calls.read_text()

    def test_nodeport_deploy_uses_explicit_readiness_and_leaves_namespace_alone(self):
        result, calls = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in calls.splitlines()]
        upgrade = next(line for line in lines if line[:2] == ["helm", "upgrade"])
        self.assertNotIn("--wait", upgrade)
        self.assertNotIn("--atomic", upgrade)
        self.assertTrue(any("rollout" in line for line in lines))
        self.assertTrue(any("endpointslice" in line for line in lines))
        self.assertTrue(any("--raw" in line for line in lines))
        self.assertFalse(any("apply" in line or "create" in line for line in lines))

    def test_normal_service_retains_helm_wait(self):
        result, calls = self.run_deploy(nodeport=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"--wait"', calls)

    def test_missing_namespace_is_created(self):
        result, calls = self.run_deploy(namespace_exists=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('["kubectl", "create", "namespace", "daedalus"]', calls)
        self.assertNotIn('"apply"', calls)

    def test_upgrade_rollout_and_health_failures_restore_and_return_failure(self):
        for failure in ("upgrade", "rollout", "health"):
            with self.subTest(failure=failure):
                result, calls = self.run_deploy(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.splitlines()[-1], "restored")
                self.assertNotIn("Switchyard is ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
