from agent.orchestrator import run_agent

LOCK = (
    "Error: Error acquiring the state lock\n\n"
    "Error message: ConditionalCheckFailedException: The conditional request failed\n"
    "Lock Info:\n  ID:        00000000-0000-0000-0000-000000000000\n  Operation: OperationTypeApply"
)
NO_CREDS = "Error: No valid credential sources found\n\nPlease see https://registry.terraform.io for more information."
IAM = ("Error: AccessDeniedException: User is not authorized to perform iam:PassRole\n"
       "on resource arn:aws:iam::123456789012:role/terraform-ci\nTerraform exited with code 1")


def run(content, source="github actions: apply"):
    return run_agent({"question": "terraform failed in CI", "observations": [{"source": source, "content": content}]})


def test_state_lock_is_diagnosed_from_the_parser_and_corroborated():
    r = run(LOCK)
    assert r["category"] == "state_lock" and r["diagnosis_source"] == "signature"
    assert "locked" in r["diagnosis"].lower()
    assert "github actions: apply: Error: Error acquiring the state lock" in r["evidence"]
    assert "retrieved: data/runbooks/terraform-state-lock.md" in r["evidence"]
    assert r["confidence"] >= 0.6
    assert "force" not in " ".join(r["suggestions"]).lower()


def test_missing_credentials_use_the_parser_summary_not_the_expired_text():
    r = run(NO_CREDS)
    assert r["category"] == "credentials_expired"
    assert r["diagnosis"] == "AWS credentials missing or expired"
    assert r["evidence"][0] == "github actions: apply: Error: No valid credential sources found"


def test_iam_denied_diagnosis_and_fix_name_the_action_and_resource():
    r = run(IAM)
    assert r["category"] == "iam_denied"
    assert "iam:PassRole on arn:aws:iam::123456789012:role/terraform-ci" in r["diagnosis"]
    assert r["terraform"]["action"] == "iam:PassRole"
    assert r["suggestions"][0].startswith("Grant only iam:PassRole on arn:aws:iam::123456789012:role/terraform-ci")
    assert r["answer"].endswith(r["suggestions"][0])


def test_generic_error_lines_in_non_terraform_logs_are_ignored():
    r = run("Error: Java heap space\nContainer terminated with exit code 137", source="kubectl logs")
    assert r["category"] == "oom_killed" and "terraform" not in r
    r = run("Error: something odd")
    assert r["category"] == "unknown" and r["confidence"] == 0.0 and "terraform" not in r


def test_signature_category_wins_over_a_conflicting_parse():
    obs = [{"source": "kubectl logs", "content": "Container terminated with exit code 137"},
           {"source": "ci", "content": LOCK}]
    r = run_agent({"question": "q", "observations": obs})
    assert r["category"] == "oom_killed" and "terraform" not in r


def test_mock_and_error_observations_are_not_parsed():
    obs = [{"source": "ci", "content": LOCK, "mock": True}, {"source": "ci", "content": LOCK, "error": True}]
    r = run_agent({"question": "q", "observations": obs})
    assert r["category"] == "unknown" and r["confidence"] == 0.0
