from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK_ROOT))

from lib import packer_runner


def load_entrypoint():
    path = PACK_ROOT / "actions" / "packer_action.py"
    spec = importlib.util.spec_from_file_location("packer_action_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load action entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE_PACKER = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
operation = args[0]
Path("fake-record.json").write_text(json.dumps({
    "args": args,
    "secret": os.environ.get("PKR_VAR_secret"),
    "provider": os.environ.get("PROVIDER_TOKEN"),
    "ambient": os.environ.get("UNRELATED_SECRET"),
    "home": os.environ.get("HOME"),
    "plugins": os.environ.get("PACKER_PLUGIN_PATH"),
}))
if any("sleep" in arg for arg in args):
    time.sleep(30)
if operation == "fix":
    print('{"fixed":true}')
elif any("failure" in arg for arg in args):
    print("synthetic failure", file=sys.stderr)
    raise SystemExit(7)
else:
    secret = os.environ.get("PKR_VAR_secret", "none")
    print(f"1700000000,,ui,say,{secret}")
    print("1700000001,docker.example,artifact,0,id,local-image")
    print(f"provider={os.environ.get('PROVIDER_TOKEN', 'none')}", file=sys.stderr)
"""


class PackerPackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.fake = self.root / "fake-packer"
        self.fake.write_text(FAKE_PACKER, encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.template = self.root / "template.pkr.hcl"
        self.template.write_text('source "null" "example" {}\n', encoding="utf-8")
        self.environment = {
            "ATTUNE_ARTIFACTS_DIR": str(self.root),
            "PACKER_EXECUTABLE": str(self.fake),
            "UNRELATED_SECRET": "ambient-must-not-pass",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, operation, params=None):
        values = {"template": self.template.name}
        values.update(params or {})
        with patch.dict(os.environ, self.environment, clear=True):
            return packer_runner.execute(operation, values)

    def test_action_metadata_contracts(self):
        documents = [path.read_text(encoding="utf-8") for path in sorted((PACK_ROOT / "actions").glob("*.yaml"))]
        self.assertEqual(
            {re.search(r"^ref: (\S+)$", document, re.MULTILINE).group(1) for document in documents},
            {"packer.build", "packer.validate", "packer.inspect", "packer.init", "packer.fix"},
        )
        for document in documents:
            self.assertIn("runner_type: python", document)
            self.assertIn("entry_point: packer_action.py", document)
            self.assertIn("parameter_delivery: stdin", document)
            self.assertIn("parameter_format: json", document)
            self.assertIn("output_format: json", document)
            self.assertIn("timeout_seconds:", document)
            self.assertNotIn("  cmd:", document)
            self.assertRegex(document, r"environment: .*secret: true")
            self.assertIn("  success:", document)
        self.assertFalse(any("packer.push" in document for document in documents))

    def test_build_uses_argv_and_environment_variables_and_redacts_output(self):
        hostile = self.root / "template;touch INJECTED.pkr.hcl"
        hostile.write_text("synthetic", encoding="utf-8")
        result = self.execute(
            "build",
            {
                "template": hostile.name,
                "variables": {"secret": "variable-secret"},
                "environment": {"PROVIDER_TOKEN": "provider-secret"},
                "only": ["docker.example"],
                "parallel_builds": 2,
            },
        )
        record = json.loads((self.root / "fake-record.json").read_text(encoding="utf-8"))
        self.assertTrue(result["success"])
        self.assertEqual(record["args"][0], "build")
        self.assertIn(str(hostile), record["args"])
        self.assertFalse((self.root / "INJECTED.pkr.hcl").exists())
        self.assertFalse(any("variable-secret" in arg for arg in record["args"]))
        self.assertEqual(record["secret"], "variable-secret")
        self.assertEqual(record["provider"], "provider-secret")
        self.assertIsNone(record["ambient"])
        self.assertNotIn("variable-secret", result["stdout"])
        self.assertNotIn("provider-secret", result["stderr"])
        self.assertIn("[REDACTED]", result["stdout"])
        self.assertEqual(result["events"][1]["data"], ["0", "id", "local-image"])
        self.assertEqual(Path(record["home"]).parent.parent, self.root)
        self.assertTrue(Path(record["plugins"]).is_relative_to(self.root))

    def test_each_current_operation_builds_expected_arguments(self):
        cases = {
            "validate": ({"syntax_only": True}, "-syntax-only"),
            "inspect": ({}, "-machine-readable"),
            "init": ({"upgrade": True}, "-upgrade"),
        }
        for operation, (params, expected) in cases.items():
            with self.subTest(operation=operation):
                result = self.execute(operation, params)
                record = json.loads((self.root / "fake-record.json").read_text(encoding="utf-8"))
                self.assertTrue(result["success"])
                self.assertEqual(record["args"][0], operation)
                self.assertIn(expected, record["args"])

    def test_fix_writes_private_confined_artifact_without_returning_contents(self):
        source = self.root / "legacy.json"
        source.write_text("{}", encoding="utf-8")
        (self.root / "converted").mkdir()
        result = self.execute("fix", {"template": source.name, "output_file": "converted/fixed.json"})
        output = Path(result["output_file"])
        self.assertTrue(result["success"])
        self.assertEqual(result["stdout"], "")
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"fixed": True})
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_nonzero_exit_is_structured_and_fails(self):
        failure = self.root / "failure.pkr.hcl"
        failure.write_text("synthetic", encoding="utf-8")
        result = self.execute("validate", {"template": failure.name})
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("synthetic failure", result["stderr"])

    def test_timeout_terminates_process_group(self):
        sleepy = self.root / "sleep.pkr.hcl"
        sleepy.write_text("synthetic", encoding="utf-8")
        result = self.execute("inspect", {"template": sleepy.name, "timeout_seconds": 1})
        self.assertFalse(result["success"])
        self.assertTrue(result["timed_out"])
        self.assertLess(result["duration_seconds"], 8)

    def test_paths_are_confined_and_symlinks_cannot_escape(self):
        outside_directory = tempfile.TemporaryDirectory()
        self.addCleanup(outside_directory.cleanup)
        outside = Path(outside_directory.name) / "outside.pkr.hcl"
        outside.write_text("synthetic", encoding="utf-8")
        link = self.root / "escape.pkr.hcl"
        link.symlink_to(outside)
        with self.assertRaisesRegex(packer_runner.PackerError, "must stay within"):
            self.execute("inspect", {"template": str(outside)})
        with self.assertRaisesRegex(packer_runner.PackerError, "must stay within"):
            self.execute("inspect", {"template": link.name})
        with self.assertRaisesRegex(packer_runner.PackerError, "output_file"):
            self.execute("fix", {"output_file": str(outside)})

    def test_packer_state_symlink_cannot_escape(self):
        outside_directory = tempfile.TemporaryDirectory()
        self.addCleanup(outside_directory.cleanup)
        (self.root / ".packer").symlink_to(outside_directory.name)
        with self.assertRaisesRegex(packer_runner.PackerError, "state directories"):
            self.execute("inspect")

    def test_reserved_environment_and_option_conflicts_are_rejected(self):
        for name in ("PACKER_PLUGIN_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "XDG_CONFIG_HOME"):
            with self.subTest(name=name), self.assertRaisesRegex(packer_runner.PackerError, "reserved"):
                self.execute("build", {"environment": {name: "/tmp/escape"}})
        with self.assertRaisesRegex(packer_runner.PackerError, "mutually exclusive"):
            self.execute("validate", {"only": ["one"], "exclude": ["two"]})

    def test_entrypoint_rejects_malformed_json_without_echoing_secret(self):
        module = load_entrypoint()
        stdin = io.StringIO('{"secret":"DO_NOT_PRINT"')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            self.assertEqual(module.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("DO_NOT_PRINT", stderr.getvalue())

    def test_source_metadata_and_notice_pin_upstream(self):
        pack = (PACK_ROOT / "pack.yaml").read_text(encoding="utf-8")
        notice = (PACK_ROOT / "NOTICE").read_text(encoding="utf-8")
        revision = "af813347fdf92cb24e466d4c0f57d9de73196407"
        self.assertIn(f'source_revision: "{revision}"', pack)
        self.assertIn('license: "Apache-2.0"', pack)
        self.assertIn(revision, notice)


if __name__ == "__main__":
    unittest.main()
