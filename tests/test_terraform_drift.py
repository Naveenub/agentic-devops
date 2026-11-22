from agent.orchestrator import run_agent
from agent.tools.terraform import parse_drift

DRIFT = """\
Note: Objects have changed outside of Terraform

Terraform detected the following changes made outside of Terraform since the
last "terraform apply" which may have affected this plan:

  # aws_instance.web has changed
  ~ resource "aws_instance" "web" {
      ~ instance_type = "t3.small" -> "t3.large"
    }

  # aws_s3_bucket.logs has been deleted
  - resource "aws_s3_bucket" "logs" {
    }

Unless you have made equivalent changes to your configuration, or ignored the
relevant attributes using ignore_changes, the following plan may include
actions to undo or respond to these changes.
"""


def run(content, source="github actions: plan"):
    return run_agent({"question": "terraform plan looks wrong", "observations": [{"source": source, "content": content}]})


def test_parse_drift_lists_changed_and_deleted_resources():
    d = parse_drift(DRIFT)
    assert d["resources"] == [{"address": "aws_instance.web", "change": "changed"},
                              {"address": "aws_s3_bucket.logs", "change": "deleted"}]
    assert d["line"] == "Note: Objects have changed outside of Terraform"
    assert "2 resource(s)" in d["summary"]


def test_resources_after_the_drift_section_are_not_drift():
    log = DRIFT + "\nTerraform will perform the following actions:\n\n  # aws_instance.other has changed\n"
    assert [r["address"] for r in parse_drift(log)["resources"]] == ["aws_instance.web", "aws_s3_bucket.logs"]


def test_no_drift_cases():
    assert parse_drift("") is None
    assert parse_drift("No changes. Your infrastructure matches the configuration.") is None
    assert parse_drift("Note: Objects have changed outside of Terraform\n\nnothing listed") is None


def test_drift_is_diagnosed_with_evidence_and_read_only_suggestions():
    r = run(DRIFT)
    assert r["category"] == "terraform_drift" and r["diagnosis_source"] == "signature"
    assert "aws_instance.web (changed)" in r["diagnosis"]
    assert "github actions: plan: Note: Objects have changed outside of Terraform" in r["evidence"]
    assert r["terraform"]["kind"] == "drift" and r["confidence"] > 0
    assert "refresh-only" in r["suggestions"][0] and "terraform apply" not in " ".join(r["suggestions"]).lower()


def test_a_real_failure_in_the_same_log_keeps_its_category():
    r = run(DRIFT + "\nError: Error acquiring the state lock\n")
    assert r["category"] == "state_lock"


def test_mock_observations_are_not_drift():
    r = run_agent({"question": "q", "observations": [{"source": "ci", "content": DRIFT, "mock": True}]})
    assert r["category"] == "unknown" and r["confidence"] == 0.0
