"""The contract the GitLab CI/CD component (gitlab.com/shipyardbuild/ci-component)
relies on. The component runs `python /opt/shipyard/plugin.py` in the image,
sets the SHIPYARD_* variables below, and publishes SHIPYARD_OUTPUT_FILE as a
dotenv artifact. Changing any of this breaks every GitLab user on the current
major version: bump the major version instead, then update the component.
"""

import os
import re

from helpers import detail_payload, env_payload

import plugin

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT_PATH = "/opt/shipyard/plugin.py"

# Exactly the variables the component's job sets (plus the masked
# SHIPYARD_API_TOKEN project variable and GitLab's own CI_* variables).
COMPONENT_ENV = {
    "SHIPYARD_API_TOKEN": "tok",
    "SHIPYARD_TIMEOUT": "7",
    "SHIPYARD_APP_NAME": "frontend",
    "SHIPYARD_API_URL": "https://shipyard.example/",
    "SHIPYARD_OUTPUT_FILE": "shipyard.env",
}
GITLAB_ENV = {
    "GITLAB_CI": "true",
    "CI_PROJECT_PATH": "acme/platform/web",
    "CI_COMMIT_BRANCH": "feature-x",
}
DOTENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^\n]*$")


def test_image_installs_the_script_where_the_component_runs_it():
    with open(os.path.join(ROOT, "Dockerfile")) as f:
        dockerfile = f.read()
    copies = re.findall(r"^COPY\s+(\S+)\s+(\S+)\s*$", dockerfile, re.MULTILINE)
    assert ("plugin.py", SCRIPT_PATH) in copies, (
        f"the GitLab component runs `python {SCRIPT_PATH}`; keep plugin.py there"
    )
    assert re.search(r"^FROM\s+python:", dockerfile, re.MULTILINE), (
        "the component runs the script with `python`"
    )


def test_component_variables_are_read():
    config = plugin.resolve_config({**COMPONENT_ENV, **GITLAB_ENV})
    assert config["api_token"] == "tok"
    assert config["timeout_minutes"] == 7
    assert config["app_name"] == "frontend"
    assert config["api_url"] == "https://shipyard.example"
    assert config["output_file"] == "shipyard.env"
    assert (config["repo_owner"], config["repo_name"], config["branch"]) == (
        "acme/platform",
        "web",
        "feature-x",
    )


def test_component_empty_app_name_means_no_filter():
    # The component always sets SHIPYARD_APP_NAME, to "" when the input is unset.
    env = {**COMPONENT_ENV, **GITLAB_ENV, "SHIPYARD_APP_NAME": ""}
    assert plugin.resolve_config(env)["app_name"] is None


def test_output_file_is_valid_dotenv_with_the_documented_names(shipyard, tmp_path):
    out = tmp_path / "shipyard.env"
    env = {
        **COMPONENT_ENV,
        **GITLAB_ENV,
        "SHIPYARD_API_URL": shipyard.url,
        "SHIPYARD_OUTPUT_FILE": str(out),
    }
    shipyard.list_responses.append((200, env_payload()))
    shipyard.detail_responses.append(
        (200, detail_payload({"api-gw": "https://api.example"}))
    )
    assert plugin.main(env) == 0

    lines = out.read_text().splitlines()
    assert lines and all(DOTENV_LINE.match(line) for line in lines), lines
    names = {line.split("=", 1)[0] for line in lines}
    assert {
        "SHIPYARD_ENVIRONMENT_URL",
        "SHIPYARD_ENVIRONMENT_ID",
        "SHIPYARD_BYPASS_TOKEN",
        "SHIPYARD_DOMAIN",
        "SHIPYARD_DOMAIN_API_GW",
    } <= names
