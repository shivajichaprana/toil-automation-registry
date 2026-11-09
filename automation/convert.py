"""Compile a reviewed JSON diagnostic procedure to an SSM Automation runbook.

For example, save this input as queue-diagnostics.json::

    {
      "description": "Inspect queue depth and the consumer instance state",
      "steps": [
        {"name": "queueDepth", "check": "sqs-queue-depth", "parameter": "QueueUrl"},
        {"name": "consumerState", "check": "ec2-instance-state", "parameter": "InstanceId"}
      ]
    }

Then run `python3 -m automation.convert queue-diagnostics.json` and review the
JSON on stdout. Supply the generated JSON to the Terraform module under a
reviewed document name. The named parameters and AutomationAssumeRole must
be supplied at execution time. Use a role scoped to the approved resources.

Each step uses a known read-only API. The compiler accepts structured checks
only. A manual recovery procedure still needs a human to decide what to do
with the diagnostic results. Conversion itself reads a file and prints JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")
MAX_STEPS = 10
MAX_DESCRIPTION = 512
ROLE = "AutomationAssumeRole"
ROLE_PATTERN = r"^arn:aws(-cn|-us-gov)?:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$"


@dataclass(frozen=True)
class Check:
    """Fixed API, target field and output shape for one diagnostic operation."""

    service: str
    api: str
    argument: str
    target_description: str
    target_pattern: str
    output_name: str
    selector: str
    fixed_inputs: dict[str, Any]
    target_is_list: bool = False


CHECKS = {
    "sqs-queue-depth": Check(
        service="sqs",
        api="GetQueueAttributes",
        argument="QueueUrl",
        target_description="Regional SQS queue URL.",
        target_pattern=(
            r"^https://sqs[.][a-z0-9-]+[.]amazonaws[.]com([.]cn)?/"
            r"[0-9]{12}/([A-Za-z0-9_-]{1,80}|[A-Za-z0-9_-]{1,75}[.]fifo)$"
        ),
        output_name="VisibleMessages",
        selector="$.Attributes.ApproximateNumberOfMessages",
        fixed_inputs={"AttributeNames": ["ApproximateNumberOfMessages"]},
    ),
    "ec2-instance-state": Check(
        service="ec2",
        api="DescribeInstanceStatus",
        argument="InstanceIds",
        target_description="EC2 instance ID in the execution Region.",
        target_pattern=r"^i-([0-9a-f]{8}|[0-9a-f]{17})$",
        output_name="InstanceState",
        selector="$.InstanceStatuses[0].InstanceState.Name",
        fixed_inputs={"IncludeAllInstances": True},
        target_is_list=True,
    ),
}


def exact_object(value: object, fields: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError(f"{context} must contain exactly: {', '.join(sorted(fields))}")
    return value


def identifier(value: object, context: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be 1–64 ASCII letters or digits, starting with a letter")
    return value


def reference(name: str) -> str:
    return "{{ " + name + " }}"


def convert(source: object) -> dict[str, Any]:
    """Validate the input and render a deterministic SSM schema 0.3 document."""
    runbook = exact_object(source, {"description", "steps"}, "Runbook")
    description = runbook["description"]
    if (
        not isinstance(description, str)
        or not 1 <= len(description.strip()) <= MAX_DESCRIPTION
        or not description.isprintable()
        or "{{" in description
        or "}}" in description
    ):
        raise ValueError("Description must be a printable single line without template markers")
    steps = runbook["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"Runbook must contain 1–{MAX_STEPS} steps")

    parameters: dict[str, dict[str, str]] = {
        ROLE: {
            "type": "String",
            "description": "ARN of the approved diagnostic execution role.",
            "allowedPattern": ROLE_PATTERN,
        }
    }
    main_steps = []
    outputs = []
    step_names: set[str] = set()

    for index, item in enumerate(steps, start=1):
        context = f"Step {index}"
        step = exact_object(item, {"name", "check", "parameter"}, context)
        name = identifier(step["name"], f"{context} name")
        parameter = identifier(step["parameter"], f"{context} parameter")
        check_name = step["check"]
        if not isinstance(check_name, str) or check_name not in CHECKS:
            raise ValueError(f"{context} check must be one of: {', '.join(sorted(CHECKS))}")
        if name in step_names:
            raise ValueError(f"{context} has a duplicate name")
        step_names.add(name)
        check = CHECKS[check_name]
        declaration = {
            "type": "String",
            "description": check.target_description,
            "allowedPattern": check.target_pattern,
        }
        if parameter in parameters and parameters[parameter] != declaration:
            raise ValueError(f"{context} reuses a parameter for an incompatible target")
        parameters[parameter] = declaration
        target = reference(parameter)
        main_steps.append(
            {
                "name": name,
                "action": "aws:executeAwsApi",
                "onFailure": "Abort",
                "timeoutSeconds": 25,
                "inputs": {
                    "Service": check.service,
                    "Api": check.api,
                    check.argument: [target] if check.target_is_list else target,
                    **check.fixed_inputs,
                },
                "outputs": [
                    {"Name": check.output_name, "Selector": check.selector, "Type": "String"}
                ],
            }
        )
        outputs.append(f"{name}.{check.output_name}")

    return {
        "schemaVersion": "0.3",
        "description": description.strip(),
        "assumeRole": reference(ROLE),
        "parameters": parameters,
        "mainSteps": main_steps,
        "outputs": outputs,
    }


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("runbook", type=Path, help="Reviewed JSON diagnostic procedure")
    args = parser.parse_args(argv)
    try:
        source = json.loads(args.runbook.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
        document = convert(source)
    except (OSError, ValueError, RecursionError) as exc:
        print(f"Could not convert runbook: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
