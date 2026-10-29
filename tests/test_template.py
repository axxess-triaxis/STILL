from pathlib import Path

from cfnlint.decode import decode

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "template.yaml"


def template():
    value, problems = decode(str(TEMPLATE_PATH))
    assert not problems
    return value


def walk(node):
    if isinstance(node, dict):
        for key, child in node.items():
            yield key, child
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


def test_no_em_dashes_anywhere():
    assert "—" not in TEMPLATE_PATH.read_text(encoding="utf-8")


def test_no_wildcard_iam_resources_or_actions():
    for key, value in walk(template()["Resources"]):
        if key in ("Resource", "Action"):
            values = value if isinstance(value, list) else [value]
            assert "*" not in values


def test_every_event_target_has_a_dead_letter_queue_and_retry_policy():
    resources = template()["Resources"]
    rules = [r for r in resources.values() if r["Type"] == "AWS::Events::Rule"]
    assert rules
    for rule in rules:
        for target in rule["Properties"]["Targets"]:
            assert "DeadLetterConfig" in target and "RetryPolicy" in target


def test_every_function_has_an_on_failure_destination():
    resources = template()["Resources"]
    functions = [r for r in resources.values() if r["Type"] == "AWS::Serverless::Function"]
    assert functions
    for function in functions:
        assert function["Properties"]["EventInvokeConfig"]["DestinationConfig"]["OnFailure"]["Type"] == "SQS"


def test_bus_is_archived_for_replay():
    resources = template()["Resources"]
    assert any(r["Type"] == "AWS::Events::Archive" for r in resources.values())


def test_secret_value_is_never_in_the_template():
    secret = template()["Resources"]["AxxessApiKeySecret"]["Properties"]
    assert "SecretString" not in secret
