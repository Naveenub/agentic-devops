import httpx
import pytest

from agent.nodes import observe as observe_node
from agent.orchestrator import run_agent
from agent.tools.github import GitHubToolError, get_ci_logs

TF_LOG = (
    "2025-01-10T10:00:00.1234567Z \x1b[31mError: AccessDeniedException: User is not authorized to perform iam:PassRole\x1b[0m\n"
    "2025-01-10T10:00:00.2000000Z on resource arn:aws:iam::123456789012:role/terraform-ci\n"
    "2025-01-10T10:00:00.3000000Z \n"
    "2025-01-10T10:00:00.4000000Z ##[error]Terraform exited with code 1.\n"
)
JOBS = {"jobs": [
    {"id": 1, "name": "lint", "conclusion": "success"},
    {"id": 2, "name": "terraform apply <script>", "conclusion": "failure"},
    {"id": 3, "name": "deploy", "conclusion": "skipped"},
    {"id": 4, "name": "timeouts", "conclusion": "timed_out"},
]}


def make_client(seen: list, log_for=lambda job_id: TF_LOG, jobs=JOBS, redirect=True):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.url.host == "api.github.com" and path.endswith("/jobs"):
            return httpx.Response(200, json=jobs)
        if request.url.host == "api.github.com" and path.endswith("/logs"):
            job_id = path.split("/")[-2]
            if not redirect:
                return httpx.Response(200, text=log_for(job_id))
            return httpx.Response(302, headers={"location": f"https://blob.example.net/logs/{job_id}?sig=abc"})
        if request.url.host == "blob.example.net":
            return httpx.Response(200, text=log_for(path.split("/")[-1]))
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_returns_cleaned_tail_of_failed_jobs_only():
    seen = []
    jobs = get_ci_logs("o/r", "99", token="t", client=make_client(seen))
    assert [j["job"] for j in jobs] == ["terraform apply script", "timeouts"]  # unsafe chars stripped
    content = jobs[0]["content"]
    assert "2025-01-10" not in content and "\x1b" not in content and "\n\n" not in content
    assert "Error: AccessDeniedException: User is not authorized to perform iam:PassRole" in content
    assert content.splitlines()[-1] == "##[error]Terraform exited with code 1."


def test_token_goes_to_github_api_but_not_to_the_signed_blob_url():
    seen = []
    get_ci_logs("o/r", "99", token="secret-token", client=make_client(seen))
    api = [r for r in seen if r.url.host == "api.github.com"]
    blob = [r for r in seen if r.url.host == "blob.example.net"]
    assert api and blob
    assert all(r.headers["authorization"] == "Bearer secret-token" for r in api)
    assert all("authorization" not in r.headers for r in blob)
    assert all(r.method == "GET" for r in seen)  # read-only


def test_only_the_last_lines_are_kept():
    big = "\n".join(f"line {i}" for i in range(1000))
    jobs = get_ci_logs("o/r", "99", token="t", client=make_client([], log_for=lambda _: big), tail=5)
    assert jobs[0]["content"].splitlines() == [f"line {i}" for i in range(995, 1000)]


def test_direct_200_log_response_is_accepted():
    jobs = get_ci_logs("o/r", "99", token="t", client=make_client([], redirect=False))
    assert "iam:PassRole" in jobs[0]["content"]


def test_missing_token_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def boom(request):
        raise AssertionError("network used without a token")

    with pytest.raises(GitHubToolError, match="GITHUB_TOKEN"):
        get_ci_logs("o/r", "1", client=httpx.Client(transport=httpx.MockTransport(boom)))


def test_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    seen = []
    get_ci_logs("o/r", "1", client=make_client(seen))
    assert seen[0].headers["authorization"] == "Bearer env-token"


@pytest.mark.parametrize("repo,run_id", [
    ("../x", "1"), ("a/b/c", "1"), ("o/r;x", "1"), ("o", "1"), ("", "1"), (None, "1"),
    ("o/r", "abc"), ("o/r", "1/../2"), ("o/r", ""), ("o/..", "1"),
])
def test_unsafe_inputs_are_rejected_before_any_request(repo, run_id):
    def boom(request):
        raise AssertionError("request made for invalid input")

    with pytest.raises(GitHubToolError):
        get_ci_logs(repo, run_id, token="t", client=httpx.Client(transport=httpx.MockTransport(boom)))


def test_api_errors_become_tool_errors():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"message": "Not Found"})))
    with pytest.raises(GitHubToolError, match="HTTP 404"):
        get_ci_logs("o/r", "1", token="t", client=c)


def test_network_errors_become_tool_errors():
    def boom(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(GitHubToolError, match="request failed"):
        get_ci_logs("o/r", "1", token="t", client=httpx.Client(transport=httpx.MockTransport(boom)))


def test_run_without_failed_jobs_is_an_error():
    ok = {"jobs": [{"id": 1, "name": "build", "conclusion": "success"}]}
    with pytest.raises(GitHubToolError, match="no failed jobs"):
        get_ci_logs("o/r", "1", token="t", client=make_client([], jobs=ok))


def test_unusable_redirect_is_rejected():
    def handler(request):
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 2, "name": "j", "conclusion": "failure"}]})
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(GitHubToolError, match="redirect"):
        get_ci_logs("o/r", "1", token="t", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_agent_diagnoses_a_github_run_end_to_end(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: make_client([]) if "timeout" in kw else real(**kw))
    r = run_agent({"question": "terraform apply failed in CI", "github": {"repo": "o/r", "run_id": "99"}})
    assert [o["source"] for o in r["observations"]] == ["github actions: terraform apply script", "github actions: timeouts"]
    assert r["category"] == "iam_denied" and r["confidence"] >= 0.6
    assert r["terraform"]["action"] == "iam:PassRole"
    assert any(e.startswith("github actions: terraform apply script: ") for e in r["evidence"])


def test_tool_failure_is_recorded_as_error_observation(monkeypatch):
    def boom(**kw):
        raise GitHubToolError("GitHub job listing failed: HTTP 403")

    monkeypatch.setattr(observe_node, "get_ci_logs", boom)
    r = run_agent({"question": "ci failed", "github": {"repo": "o/r", "run_id": "1"}})
    assert r["observations"] == [{"source": "github actions", "content": "GitHub job listing failed: HTTP 403", "error": True}]
    assert r["category"] == "unknown" and r["confidence"] == 0.0
