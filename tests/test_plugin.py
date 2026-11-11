import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import plugin


def env_payload(**attrs):
    base = {
        "url": "https://app-pr-1.example.shipyard.host",
        "bypass_token": "bypass-123",
        "ready": True,
        "stopped": False,
        "retired": False,
        "processing": False,
        "projects": [],
    }
    base.update(attrs)
    return {"data": [{"id": "env-uuid", "attributes": base}]}


def detail_payload(additional_urls=None):
    return {
        "data": {
            "id": "env-uuid",
            "attributes": {"additional_urls": additional_urls or {}},
        }
    }


class FakeShipyard:
    """Local HTTP server that replays queued responses.

    GET /api/v1/environment?... (list) pops list_responses. Like the real API,
    the list response has no additional_urls; GET /api/v1/environment/<id>
    (detail) pops detail_responses, defaulting to no additional URLs.
    """

    def __init__(self):
        self.list_responses = []  # list of (status, body)
        self.detail_responses = []
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append(("GET", self.path, headers))
                if urlparse(self.path).path == "/api/v1/environment":
                    status, body = fake.list_responses.pop(0)
                elif fake.detail_responses:
                    status, body = fake.detail_responses.pop(0)
                else:
                    status, body = 200, detail_payload()
                self._reply(status, body)

            def do_POST(self):
                fake.requests.append(("POST", self.path, dict(self.headers)))
                self._reply(200, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def list_requests(self):
        return [
            r
            for r in self.requests
            if r[0] == "GET" and urlparse(r[1]).path == "/api/v1/environment"
        ]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def shipyard():
    fake = FakeShipyard()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(plugin.time, "sleep", lambda s: None)
    monkeypatch.setattr(plugin, "REQUEST_RETRY_DELAY", 0)


@pytest.fixture
def harness_env(tmp_path, shipyard):
    return {
        "PLUGIN_API_TOKEN": "tok",
        "PLUGIN_API_URL": shipyard.url,
        "DRONE_REPO_OWNER": "acme",
        "DRONE_REPO_NAME": "web",
        "DRONE_SOURCE_BRANCH": "feature-x",
        "DRONE_BRANCH": "main",
        "DRONE_OUTPUT": str(tmp_path / "out.env"),
        "HARNESS_OUTPUT_SECRET_FILE": str(tmp_path / "secret.env"),
    }


def read_env(path):
    with open(path) as f:
        return dict(line.rstrip("\n").split("=", 1) for line in f if line.strip())


# --- config resolution ----------------------------------------------------


def test_pr_build_uses_source_branch_not_target(harness_env):
    assert plugin.resolve_config(harness_env)["branch"] == "feature-x"


def test_push_build_falls_back_to_commit_branch(harness_env):
    del harness_env["DRONE_SOURCE_BRANCH"]
    harness_env["DRONE_COMMIT_BRANCH"] = "release"
    assert plugin.resolve_config(harness_env)["branch"] == "release"


def test_repo_comes_from_clone_url_not_connector_owner(harness_env):
    # Seen on Harness: an account-level connector owned by a user reports the
    # user as DRONE_REPO_OWNER for a repo that lives in an org.
    harness_env.update(
        DRONE_REPO_OWNER="some-user",
        DRONE_REPO_NAME="web",
        DRONE_REMOTE_URL="https://github.com/acme-org/web.git",
        DRONE_REPO_LINK="https://github.com/acme-org/web",
    )
    config = plugin.resolve_config(harness_env)
    assert (config["repo_owner"], config["repo_name"]) == ("acme-org", "web")


def test_repo_link_used_when_no_remote_url(harness_env):
    harness_env.update(
        DRONE_REPO_OWNER="some-user", DRONE_REPO_LINK="https://github.com/acme-org/web"
    )
    assert plugin.resolve_config(harness_env)["repo_owner"] == "acme-org"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/acme/web.git", ("acme", "web")),
        ("https://github.com/acme/web", ("acme", "web")),
        ("git@github.com:acme/web.git", ("acme", "web")),
        ("https://gitlab.com/acme/team/web.git", ("acme/team", "web")),
        ("https://bitbucket.org/acme/web.git", ("acme", "web")),
        ("https://dev.azure.com/acme/proj/_git/web", (None, None)),
        ("https://github.com/acme", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_repo_from_url(url, expected):
    assert plugin.repo_from_url(url) == expected


def test_settings_override_drone_vars(harness_env):
    harness_env.update(
        PLUGIN_REPO_OWNER="other", PLUGIN_REPO_NAME="api", PLUGIN_BRANCH="b2"
    )
    config = plugin.resolve_config(harness_env)
    assert (config["repo_owner"], config["repo_name"], config["branch"]) == (
        "other",
        "api",
        "b2",
    )


def test_shipyard_env_vars_are_fallbacks(harness_env):
    del harness_env["PLUGIN_API_TOKEN"]
    harness_env.update(
        SHIPYARD_API_TOKEN="env-tok", SHIPYARD_TIMEOUT="5", SHIPYARD_APP_NAME="app"
    )
    config = plugin.resolve_config(harness_env)
    assert config["api_token"] == "env-tok"
    assert config["timeout_minutes"] == 5
    assert config["app_name"] == "app"


def test_defaults(harness_env):
    del harness_env["PLUGIN_API_URL"]
    config = plugin.resolve_config(harness_env)
    assert config["api_url"] == "https://shipyard.build"
    assert config["timeout_minutes"] == 60
    assert config["app_name"] is None


@pytest.mark.parametrize(
    "change, message",
    [
        ({"PLUGIN_API_TOKEN": ""}, "No Shipyard API token"),
        ({"PLUGIN_TIMEOUT_MINUTES": "soon"}, "not an integer"),
        ({"DRONE_REPO_NAME": ""}, "repo_name"),
        ({"DRONE_SOURCE_BRANCH": "refs/tags/v1", "DRONE_BRANCH": ""}, "tag"),
    ],
)
def test_bad_config(harness_env, change, message):
    harness_env.update(change)
    with pytest.raises(plugin.PluginError, match=message):
        plugin.resolve_config(harness_env)


# --- end to end against the fake API -------------------------------------


def test_ready_environment_writes_outputs_and_secret(shipyard, harness_env):
    shipyard.list_responses.append(
        (
            200,
            env_payload(
                projects=[
                    {"commit_hash": "aaa", "pull_request_number": None},
                    {"commit_hash": "bbb", "pull_request_number": 7},
                ],
            ),
        )
    )
    shipyard.detail_responses.append(
        (200, detail_payload({"api-gw": "https://api.example"}))
    )
    assert plugin.main(harness_env) == 0

    method, path, headers = shipyard.requests[0]
    query = parse_qs(urlparse(path).query)
    assert method == "GET" and urlparse(path).path == "/api/v1/environment"
    assert query == {
        "repo_owner": ["acme"],
        "repo_name": ["web"],
        "branch": ["feature-x"],
    }
    assert headers["x-api-token"] == "tok"

    outputs = read_env(harness_env["DRONE_OUTPUT"])
    assert outputs == {
        "SHIPYARD_ENVIRONMENT_URL": "https://app-pr-1.example.shipyard.host",
        "SHIPYARD_ENVIRONMENT_ID": "env-uuid",
        "SHIPYARD_ENVIRONMENT_READY": "True",
        "SHIPYARD_ENVIRONMENT_RETIRED": "False",
        "SHIPYARD_DOMAIN": "https://app-pr-1.example.shipyard.host",
        "SHIPYARD_ENVIRONMENT_COMMIT_HASH": "bbb",
        "SHIPYARD_DOMAIN_API_GW": "https://api.example",
    }
    assert "SHIPYARD_BYPASS_TOKEN" not in outputs
    assert read_env(harness_env["HARNESS_OUTPUT_SECRET_FILE"]) == {
        "SHIPYARD_BYPASS_TOKEN": "bypass-123"
    }


def test_outputs_written_without_commit_hash(shipyard, harness_env):
    # Regression for the GitHub Action, which writes nothing in this case.
    shipyard.list_responses.append((200, env_payload(projects=[{}, {}])))
    assert plugin.main(harness_env) == 0
    outputs = read_env(harness_env["DRONE_OUTPUT"])
    assert outputs["SHIPYARD_ENVIRONMENT_ID"] == "env-uuid"
    assert "SHIPYARD_ENVIRONMENT_COMMIT_HASH" not in outputs


def test_token_falls_back_to_plain_output_without_secret_file(
    shipyard, harness_env, capsys
):
    del harness_env["HARNESS_OUTPUT_SECRET_FILE"]
    shipyard.list_responses.append((200, env_payload()))
    assert plugin.main(harness_env) == 0
    assert (
        read_env(harness_env["DRONE_OUTPUT"])["SHIPYARD_BYPASS_TOKEN"] == "bypass-123"
    )
    assert "not masked" in capsys.readouterr().out


def test_app_name_filter_is_sent(shipyard, harness_env):
    harness_env["PLUGIN_APP_NAME"] = "frontend"
    shipyard.list_responses.append((200, env_payload()))
    assert plugin.main(harness_env) == 0
    assert parse_qs(urlparse(shipyard.requests[0][1]).query)["name"] == ["frontend"]


def test_waits_until_ready(shipyard, harness_env):
    shipyard.list_responses += [
        (200, env_payload(ready=False, processing=True)),
        (200, env_payload(ready=False, processing=True)),
        (200, env_payload(ready=True)),
    ]
    assert plugin.main(harness_env) == 0
    assert len(shipyard.list_requests()) == 3


def test_retired_environment_is_restarted_once(shipyard, harness_env):
    shipyard.list_responses += [
        (200, env_payload(ready=False, retired=True)),
        (200, env_payload(ready=False, retired=True, processing=True)),
        (200, env_payload(ready=True)),
    ]
    assert plugin.main(harness_env) == 0
    posts = [r for r in shipyard.requests if r[0] == "POST"]
    assert [p[1] for p in posts] == ["/api/v1/environment/env-uuid/restart"]


def test_stopped_idle_environment_fails(shipyard, harness_env, capsys):
    shipyard.list_responses.append(
        (200, env_payload(ready=False, stopped=True, processing=False))
    )
    assert plugin.main(harness_env) == 1
    assert "stopped and no build is running" in capsys.readouterr().out


def test_no_environment_found(shipyard, harness_env, capsys):
    shipyard.list_responses.append((200, {"data": []}))
    assert plugin.main(harness_env) == 1
    assert "No Shipyard environment found for acme/web on branch feature-x" in (
        capsys.readouterr().out
    )


def test_timeout(shipyard, harness_env, monkeypatch, capsys):
    harness_env["PLUGIN_TIMEOUT_MINUTES"] = "1"
    shipyard.list_responses += [(200, env_payload(ready=False, processing=True))] * 10
    ticks = iter(range(0, 1000, 45))
    monkeypatch.setattr(plugin.time, "monotonic", lambda: next(ticks))
    assert plugin.main(harness_env) == 1
    assert "Timed out after 1 minutes" in capsys.readouterr().out


def test_client_error_is_not_retried(shipyard, harness_env, capsys):
    shipyard.list_responses.append((401, {"errors": [{"title": "Invalid token"}]}))
    assert plugin.main(harness_env) == 1
    assert len(shipyard.requests) == 1
    assert "401" in capsys.readouterr().out


def test_server_error_is_retried(shipyard, harness_env):
    shipyard.list_responses += [(502, {}), (200, env_payload())]
    assert plugin.main(harness_env) == 0
    assert len(shipyard.list_requests()) == 2


def test_harness_output_file_used_when_no_drone_output(shipyard, harness_env, tmp_path):
    del harness_env["DRONE_OUTPUT"]
    harness_env["HARNESS_OUTPUT_FILE"] = str(tmp_path / "harness-out.env")
    shipyard.list_responses.append((200, env_payload()))
    assert plugin.main(harness_env) == 0
    outputs = read_env(harness_env["HARNESS_OUTPUT_FILE"])
    assert outputs["SHIPYARD_ENVIRONMENT_ID"] == "env-uuid"


def test_missing_output_file_fails(shipyard, harness_env, capsys):
    del harness_env["DRONE_OUTPUT"]
    shipyard.list_responses.append((200, env_payload()))
    assert plugin.main(harness_env) == 1
    assert "does not support step output variables" in capsys.readouterr().out


def test_every_additional_url_becomes_an_output(shipyard, harness_env):
    shipyard.list_responses.append((200, env_payload()))
    shipyard.detail_responses.append(
        (
            200,
            detail_payload(
                {
                    "frontend": "https://web.example",
                    "api": "https://api.example",
                    "admin.panel": "https://admin.example",
                }
            ),
        )
    )
    assert plugin.main(harness_env) == 0
    detail = [r for r in shipyard.requests if r[1] == "/api/v1/environment/env-uuid"]
    assert len(detail) == 1
    outputs = read_env(harness_env["DRONE_OUTPUT"])
    assert {k: v for k, v in outputs.items() if k.startswith("SHIPYARD_DOMAIN_")} == {
        "SHIPYARD_DOMAIN_FRONTEND": "https://web.example",
        "SHIPYARD_DOMAIN_API": "https://api.example",
        "SHIPYARD_DOMAIN_ADMIN_PANEL": "https://admin.example",
    }


def test_additional_urls_come_from_detail_not_list(shipyard, harness_env):
    # The real list endpoint omits additional_urls; even if it sent them, the
    # detail endpoint is the source of truth.
    shipyard.list_responses.append(
        (200, env_payload(additional_urls={"stale": "https://stale.example"}))
    )
    assert plugin.main(harness_env) == 0
    outputs = read_env(harness_env["DRONE_OUTPUT"])
    assert not [k for k in outputs if k.startswith("SHIPYARD_DOMAIN_")]


def test_detail_failure_still_writes_main_outputs(shipyard, harness_env, capsys):
    shipyard.list_responses.append((200, env_payload()))
    shipyard.detail_responses += [(502, {})] * 3
    assert plugin.main(harness_env) == 0
    outputs = read_env(harness_env["DRONE_OUTPUT"])
    assert (
        outputs["SHIPYARD_ENVIRONMENT_URL"] == "https://app-pr-1.example.shipyard.host"
    )
    assert read_env(harness_env["HARNESS_OUTPUT_SECRET_FILE"])
    assert "could not read the environment's additional URLs" in capsys.readouterr().out


# --- GitLab CI ---------------------------------------------------------------


@pytest.fixture
def gitlab_env(tmp_path, shipyard):
    return {
        "GITLAB_CI": "true",
        "SHIPYARD_API_TOKEN": "tok",
        "SHIPYARD_API_URL": shipyard.url,
        "CI_PROJECT_PATH": "acme/platform/web",
        "CI_PROJECT_NAME": "Web App",
        "CI_PROJECT_NAMESPACE": "acme/platform",
        "CI_COMMIT_BRANCH": "feature-x",
        "CI_COMMIT_REF_NAME": "feature-x",
        "SHIPYARD_OUTPUT_FILE": str(tmp_path / "shipyard.env"),
    }


def test_gitlab_repo_from_project_path_with_subgroups(gitlab_env):
    # CI_PROJECT_NAME is the display name; Shipyard stores the project path.
    config = plugin.resolve_config(gitlab_env)
    assert (config["repo_owner"], config["repo_name"], config["branch"]) == (
        "acme/platform",
        "web",
        "feature-x",
    )


def test_gitlab_merge_request_uses_source_branch(gitlab_env):
    del gitlab_env["CI_COMMIT_BRANCH"]
    gitlab_env.update(
        CI_MERGE_REQUEST_SOURCE_BRANCH_NAME="feature-x",
        CI_MERGE_REQUEST_TARGET_BRANCH_NAME="main",
        CI_COMMIT_REF_NAME="refs/merge-requests/7/head",
    )
    assert plugin.resolve_config(gitlab_env)["branch"] == "feature-x"


def test_gitlab_tag_pipeline_is_rejected(gitlab_env):
    del gitlab_env["CI_COMMIT_BRANCH"]
    gitlab_env.update(CI_COMMIT_TAG="v1.2.3", CI_COMMIT_REF_NAME="v1.2.3")
    with pytest.raises(plugin.PluginError, match="tag"):
        plugin.resolve_config(gitlab_env)


def test_gitlab_ignores_drone_vars(gitlab_env):
    gitlab_env.update(DRONE_REPO_OWNER="other", DRONE_SOURCE_BRANCH="other-branch")
    config = plugin.resolve_config(gitlab_env)
    assert (config["repo_owner"], config["branch"]) == ("acme/platform", "feature-x")


def test_gitlab_shipyard_env_overrides(gitlab_env):
    gitlab_env.update(
        SHIPYARD_REPO_OWNER="o", SHIPYARD_REPO_NAME="n", SHIPYARD_BRANCH="b"
    )
    config = plugin.resolve_config(gitlab_env)
    assert (config["repo_owner"], config["repo_name"], config["branch"]) == (
        "o",
        "n",
        "b",
    )


def test_gitlab_writes_everything_to_dotenv(shipyard, gitlab_env, capsys):
    shipyard.list_responses.append((200, env_payload()))
    shipyard.detail_responses.append(
        (200, detail_payload({"api": "https://api.example"}))
    )
    assert plugin.main(gitlab_env) == 0
    query = parse_qs(urlparse(shipyard.list_requests()[0][1]).query)
    assert query["repo_owner"] == ["acme/platform"]
    assert query["repo_name"] == ["web"]
    dotenv = read_env(gitlab_env["SHIPYARD_OUTPUT_FILE"])
    assert (
        dotenv["SHIPYARD_ENVIRONMENT_URL"] == "https://app-pr-1.example.shipyard.host"
    )
    assert dotenv["SHIPYARD_BYPASS_TOKEN"] == "bypass-123"
    assert dotenv["SHIPYARD_DOMAIN_API"] == "https://api.example"
    out = capsys.readouterr().out
    assert "bypass-123" not in out
    assert "HARNESS_OUTPUT_SECRET_FILE" not in out


def test_gitlab_default_output_file(shipyard, gitlab_env, tmp_path, monkeypatch):
    del gitlab_env["SHIPYARD_OUTPUT_FILE"]
    monkeypatch.chdir(tmp_path)
    shipyard.list_responses.append((200, env_payload()))
    assert plugin.main(gitlab_env) == 0
    assert read_env(tmp_path / "shipyard.env")["SHIPYARD_ENVIRONMENT_ID"] == "env-uuid"
