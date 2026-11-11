"""Shipyard plugin for Harness CI.

Waits for the Shipyard environment of the current repo and branch to be
ready, then exports its URL, ID and bypass token as step outputs.

Settings (Harness passes each `settings:` key as a PLUGIN_* env var):
  api_token        PLUGIN_API_TOKEN, falls back to SHIPYARD_API_TOKEN (required)
  timeout_minutes  PLUGIN_TIMEOUT_MINUTES, falls back to SHIPYARD_TIMEOUT (default 60)
  app_name         PLUGIN_APP_NAME, falls back to SHIPYARD_APP_NAME
  api_url          PLUGIN_API_URL, falls back to SHIPYARD_API_URL (default https://shipyard.build)
  repo_owner, repo_name, branch
                   override what Harness reports: the repo comes from the clone
                   URL (DRONE_REMOTE_URL / DRONE_REPO_LINK), the branch from
                   DRONE_SOURCE_BRANCH, falling back to DRONE_COMMIT_BRANCH

Outputs:
  $DRONE_OUTPUT                 SHIPYARD_ENVIRONMENT_URL, _ID, _READY, _RETIRED,
                                _COMMIT_HASH, SHIPYARD_DOMAIN, SHIPYARD_DOMAIN_*
  $HARNESS_OUTPUT_SECRET_FILE   SHIPYARD_BYPASS_TOKEN (masked in logs). When
                                Harness doesn't provide this file, the token
                                goes to $DRONE_OUTPUT instead, unmasked.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

DEFAULT_API_URL = "https://shipyard.build"
DEFAULT_TIMEOUT_MINUTES = 60
POLL_SECONDS = 15
REQUEST_RETRIES = 3
REQUEST_RETRY_DELAY = 5
USER_AGENT = "shipyard-harness-plugin"


class PluginError(Exception):
    pass


def log(msg):
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] {msg}", flush=True)


def _error_title(http_error):
    """The first JSON:API error title from an HTTP error body, if there is one."""
    try:
        body = json.loads(http_error.read() or b"{}")
        return body["errors"][0]["title"]
    except (ValueError, KeyError, IndexError, TypeError):
        return http_error.reason


def setting(name, fallback_env=None, env=None):
    """Read a plugin setting, then its SHIPYARD_* fallback. Empty means unset."""
    env = os.environ if env is None else env
    value = env.get(f"PLUGIN_{name.upper()}", "").strip()
    if not value and fallback_env:
        value = env.get(fallback_env, "").strip()
    return value or None


def repo_from_url(url):
    """(owner, name) from a git URL, or (None, None) if it can't be parsed.

    Handles https://host/owner/repo(.git), git@host:owner/repo(.git), and
    GitLab subgroups (owner is everything before the repo name). Azure Repos
    URLs (/_git/) aren't in owner/repo form, so they return (None, None).
    """
    if not url:
        return None, None
    url = url.strip()
    if "://" in url:
        path = urllib.parse.urlparse(url).path
    elif ":" in url:  # scp-style ssh: git@github.com:owner/repo.git
        path = url.split(":", 1)[1]
    else:
        return None, None
    path = path.strip("/").removesuffix(".git")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or "_git" in parts:
        return None, None
    return "/".join(parts[:-1]), parts[-1]


def resolve_config(env=None):
    env = os.environ if env is None else env

    api_token = setting("api_token", "SHIPYARD_API_TOKEN", env)
    if not api_token:
        raise PluginError(
            "No Shipyard API token: set the api_token setting or SHIPYARD_API_TOKEN"
        )

    timeout = setting("timeout_minutes", "SHIPYARD_TIMEOUT", env)
    if timeout is None:
        timeout_minutes = DEFAULT_TIMEOUT_MINUTES
    else:
        try:
            timeout_minutes = int(timeout)
        except ValueError:
            raise PluginError(f'timeout_minutes ("{timeout}") is not an integer')

    # DRONE_REPO_OWNER is the *connector's* account, not the repo's: with an
    # account-level connector owned by a user, a repo in an org reports the
    # user. The clone URL always names the real repo, so prefer it.
    url_owner, url_name = repo_from_url(
        env.get("DRONE_REMOTE_URL") or env.get("DRONE_REPO_LINK")
    )
    repo_owner = (
        setting("repo_owner", env=env) or url_owner or env.get("DRONE_REPO_OWNER")
    )
    repo_name = setting("repo_name", env=env) or url_name or env.get("DRONE_REPO_NAME")
    # DRONE_SOURCE_BRANCH is the PR head branch, and matches the pushed branch
    # on push builds. DRONE_BRANCH/DRONE_COMMIT_BRANCH is the PR *target*
    # branch, so it is only a fallback.
    branch = (
        setting("branch", env=env)
        or env.get("DRONE_SOURCE_BRANCH")
        or env.get("DRONE_COMMIT_BRANCH")
        or env.get("DRONE_BRANCH")
    )

    missing = [
        name
        for name, value in (
            ("repo_owner", repo_owner),
            ("repo_name", repo_name),
            ("branch", branch),
        )
        if not value
    ]
    if missing:
        raise PluginError(
            "Could not work out {} from the Harness build; set them as plugin "
            "settings".format(", ".join(missing))
        )
    if branch.startswith("refs/tags/"):
        raise PluginError(
            f"This build is for a tag ({branch}); Shipyard environments are per branch"
        )

    return {
        "api_token": api_token,
        "api_url": (
            setting("api_url", "SHIPYARD_API_URL", env) or DEFAULT_API_URL
        ).rstrip("/"),
        "timeout_minutes": timeout_minutes,
        "app_name": setting("app_name", "SHIPYARD_APP_NAME", env),
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
    }


class ShipyardClient:
    def __init__(self, api_url, api_token, sleep=None):
        self.api_url = api_url
        self.api_token = api_token
        self.sleep = sleep or time.sleep

    def _request(self, method, path, query=None):
        url = self.api_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(
            url,
            method=method,
            headers={
                "x-api-token": self.api_token,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        last_error = None
        for attempt in range(REQUEST_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                # 4xx won't fix itself (bad token, bad filter): fail now.
                if 400 <= e.code < 500:
                    raise PluginError(
                        f"Shipyard API returned {e.code} for {method} {path}: "
                        f"{_error_title(e)}"
                    )
                last_error = f"HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                last_error = str(e)
            if attempt < REQUEST_RETRIES - 1:
                wait = REQUEST_RETRY_DELAY * (2**attempt)
                log(f"Request failed ({last_error}), retrying in {wait}s...")
                self.sleep(wait)
        raise PluginError(
            f"Shipyard API {method} {path} failed after {REQUEST_RETRIES} attempts: {last_error}"
        )

    def find_environment(self, repo_owner, repo_name, branch, app_name=None):
        query = {"repo_owner": repo_owner, "repo_name": repo_name, "branch": branch}
        if app_name:
            query["name"] = app_name
        response = self._request("GET", "/api/v1/environment", query)

        errors = response.get("errors")
        if errors:
            raise PluginError(f"Shipyard API error: {errors[0].get('title')}")
        data = response.get("data") or []
        if not data:
            raise PluginError(
                f"No Shipyard environment found for {repo_owner}/{repo_name} "
                f"on branch {branch}" + (f" (app {app_name})" if app_name else "")
            )
        try:
            env_id = data[0]["id"]
            attributes = data[0]["attributes"]
        except (KeyError, TypeError):
            raise PluginError("Unexpected response shape from the Shipyard API")
        for field in ("bypass_token", "url", "ready", "stopped", "retired"):
            if field not in attributes:
                raise PluginError(f"Shipyard API response is missing {field}")
        return env_id, attributes

    def restart_environment(self, env_id):
        self._request("POST", f"/api/v1/environment/{env_id}/restart")


def wait_for_environment(client, config, sleep=None, clock=None):
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    lookup = (
        config["repo_owner"],
        config["repo_name"],
        config["branch"],
        config["app_name"],
    )
    env_id, attrs = client.find_environment(*lookup)
    log(f"Found Shipyard environment {env_id}")

    start = clock()
    deadline = start + config["timeout_minutes"] * 60
    restarted = False

    while not attrs["ready"]:
        if clock() > deadline:
            raise PluginError(
                f"Timed out after {config['timeout_minutes']} minutes waiting for "
                f"environment {env_id}"
            )
        # A retired environment gets one restart; a stopped one with no build
        # running will never become ready on its own.
        if attrs["retired"] and not restarted:
            client.restart_environment(env_id)
            restarted = True
            log("Environment was retired; restarted it")
        elif attrs["stopped"] and not attrs.get("processing"):
            raise PluginError(
                f"Environment {env_id} is stopped and no build is running"
            )

        log(f"Waiting for Shipyard environment... ({int(clock() - start)}s elapsed)")
        sleep(POLL_SECONDS)
        env_id, attrs = client.find_environment(*lookup)

    return env_id, attrs


def commit_hash_of(attrs):
    """The commit of the PR's project, else the first project's."""
    projects = attrs.get("projects") or []
    with_pr = [p for p in projects if p.get("pull_request_number") is not None]
    project = with_pr[0] if with_pr else (projects[0] if projects else {})
    return project.get("commit_hash") or attrs.get("commit_hash")


def build_outputs(env_id, attrs):
    outputs = {
        "SHIPYARD_ENVIRONMENT_URL": attrs["url"],
        "SHIPYARD_ENVIRONMENT_ID": env_id,
        "SHIPYARD_ENVIRONMENT_READY": attrs["ready"],
        "SHIPYARD_ENVIRONMENT_RETIRED": attrs["retired"],
        "SHIPYARD_DOMAIN": attrs["url"],
    }
    commit_hash = commit_hash_of(attrs)
    if commit_hash:
        outputs["SHIPYARD_ENVIRONMENT_COMMIT_HASH"] = commit_hash
    for name, url in (attrs.get("additional_urls") or {}).items():
        outputs["SHIPYARD_DOMAIN_" + name.upper().replace("-", "_")] = url
    return outputs, {"SHIPYARD_BYPASS_TOKEN": attrs["bypass_token"]}


def write_env_file(path, values):
    with open(path, "a") as f:
        f.writelines(f"{key}={value}\n" for key, value in values.items())


def write_outputs(outputs, secrets, env=None):
    env = os.environ if env is None else env
    # Harness sets DRONE_OUTPUT on classic Plugin steps and on New Experience
    # run steps; HARNESS_OUTPUT_FILE is the same file under Harness's own name.
    output_path = env.get("DRONE_OUTPUT") or env.get("HARNESS_OUTPUT_FILE")
    secret_path = env.get("HARNESS_OUTPUT_SECRET_FILE")

    if not output_path:
        raise PluginError(
            "Neither DRONE_OUTPUT nor HARNESS_OUTPUT_FILE is set; this build "
            "infrastructure does not support step output variables"
        )
    if secret_path:
        write_env_file(secret_path, secrets)
    else:
        log(
            "WARNING: HARNESS_OUTPUT_SECRET_FILE is not set, so the bypass token is "
            "written as a plain output variable and is not masked in logs. Ask "
            "Harness to enable output secrets for plugins (CI_ENABLE_OUTPUT_SECRETS)."
        )
        outputs = {**outputs, **secrets}
    write_env_file(output_path, outputs)


def main(env=None):
    try:
        config = resolve_config(env)
        log(
            "Looking for the Shipyard environment of {repo_owner}/{repo_name} "
            "on branch {branch}".format(**config)
        )
        client = ShipyardClient(config["api_url"], config["api_token"])
        env_id, attrs = wait_for_environment(client, config)
        outputs, secrets = build_outputs(env_id, attrs)
        write_outputs(outputs, secrets, env)
    except PluginError as e:
        log(f"ERROR: {e}")
        return 1
    log(f"Shipyard environment {env_id} is ready at {attrs['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
