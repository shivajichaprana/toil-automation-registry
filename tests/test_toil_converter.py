"""Regression tests for structured diagnostic runbook conversion."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from automation.convert import convert, unique_keys

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return {
        "description": "Inspect queue and consumer state",
        "steps": [
            {"name": "queueDepth", "check": "sqs-queue-depth", "parameter": "QueueUrl"},
            {
                "name": "consumerState",
                "check": "ec2-instance-state",
                "parameter": "InstanceId",
            },
        ],
    }


class ConverterRegressionTests(unittest.TestCase):
    def test_deterministic_readonly_output(self):
        first = convert(sample())
        second = convert(sample())
        self.assertEqual(first, second)
        self.assertEqual(first["schemaVersion"], "0.3")
        self.assertEqual(first["assumeRole"], "{{ AutomationAssumeRole }}")
        self.assertNotIn("default", first["parameters"]["AutomationAssumeRole"])
        self.assertEqual(
            [s["inputs"]["Api"] for s in first["mainSteps"]],
            ["GetQueueAttributes", "DescribeInstanceStatus"],
        )
        self.assertTrue(
            all(
                s["action"] == "aws:executeAwsApi" and s["onFailure"] == "Abort"
                for s in first["mainSteps"]
            )
        )

    def test_negative_inputs(self):
        cases = [
            None,
            [],
            {},
            {"description": "x", "steps": []},
            {"description": "x", "steps": sample()["steps"] * 6},
        ]
        for description in ["", "line\nline", "{{ parameter }}", "x" * 513]:
            data = sample()
            data["description"] = description
            cases.append(data)
        for field, value in [
            ("name", "1bad"),
            ("name", "a-b"),
            ("check", "shell-command"),
            ("parameter", "AutomationAssumeRole"),
        ]:
            data = sample()
            data["steps"][0][field] = value
            cases.append(data)
        data = sample()
        data["steps"][1]["name"] = data["steps"][0]["name"]
        cases.append(data)
        data = sample()
        data["steps"][1]["parameter"] = data["steps"][0]["parameter"]
        cases.append(data)
        data = sample()
        data["steps"][0]["command"] = "unexpected"
        cases.append(data)
        for i, data in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(ValueError):
                convert(data)

    def test_duplicate_json_keys_refused(self):
        with self.assertRaises(ValueError):
            json.loads(
                '{"description":"first","description":"second"}',
                object_pairs_hook=unique_keys,
            )

    def test_input_unchanged(self):
        data = sample()
        original = copy.deepcopy(data)
        convert(data)
        self.assertEqual(data, original)

    def test_shared_compatible_parameter(self):
        data = sample()
        data["steps"] = [data["steps"][0], {**data["steps"][0], "name": "secondRead"}]
        result = convert(data)
        self.assertEqual(set(result["parameters"]), {"AutomationAssumeRole", "QueueUrl"})

    def test_cli_success_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            path.write_text(json.dumps(sample()))
            ok = subprocess.run(
                [sys.executable, "-B", "-m", "automation.convert", str(path)],
                check=False,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(json.loads(ok.stdout), convert(sample()))
            path.write_text("{broken")
            bad = subprocess.run(
                [sys.executable, "-B", "-m", "automation.convert", str(path)],
                check=False,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(bad.stdout, "")


if __name__ == "__main__":
    unittest.main()
