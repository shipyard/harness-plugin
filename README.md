# Shipyard Harness CI Plugin

[![GitHub License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

Use Shipyard with [Harness CI](https://www.harness.io/products/continuous-integration) to run jobs against the ephemeral environment Shipyard deploys for your branch or pull request, authenticating into it with a bypass token.

The plugin waits for the Shipyard environment of the build's repo and branch to be ready, then exports its URL and bypass token as step outputs, so later steps can run e2e tests where OAuth would normally be required. It works the same way as the Shipyard [GitHub Action](https://github.com/shipyard/github-action) and [CircleCI orb](https://github.com/shipyardbuild/circleci-orb).

## How to use

1. Create a Harness text secret holding your Shipyard API token (from your organization's settings page in Shipyard), e.g. `shipyard_api_token`.
2. Add the plugin as a step in your CI stage, then read its outputs in a later step. The YAML depends on which Harness pipeline editor your account uses.

### Harness New Experience (v1 pipeline YAML)

Run the image in a `run` step and pass settings as `PLUGIN_*` environment variables:

```yaml
pipeline:
  clone:
    connector: my_github_connector
    repo: my-org/my-repo
  stages:
    - name: e2e
      id: e2e
      steps:
        - name: shipyard
          id: shipyard
          run:
            container:
              image: ghcr.io/shipyard/harness-plugin:1
            script: python /opt/shipyard/plugin.py
            env:
              PLUGIN_API_TOKEN: <+secrets.getValue("shipyard_api_token")>
              PLUGIN_TIMEOUT_MINUTES: "30"
        - name: e2e_tests
          id: e2e_tests
          run:
            container:
              image: node:20
            env:
              CYPRESS_BASE_URL: <+steps.shipyard.output.outputVariables.SHIPYARD_ENVIRONMENT_URL>
              CYPRESS_BYPASS_TOKEN: <+steps.shipyard.output.outputVariables.SHIPYARD_BYPASS_TOKEN>
            script: npm ci && npm test
```

- Give every step and stage that uses an expression an `id`, or Harness won't save the pipeline.
- `container` must be an object with `image`; the string shorthand fails validation.
- Use the `<+...>` expressions shown. In our testing, the `${{ secrets.NAME }}` and `${{ steps.ID.output.NAME }}` forms resolved to empty strings.

A full pipeline is in [`examples/e2e-pipeline-v1.yaml`](examples/e2e-pipeline-v1.yaml).

### Harness CI (classic pipeline YAML)

Add a **Plugin** step; its `settings` become `PLUGIN_*` environment variables:

```yaml
- step:
    type: Plugin
    name: Fetch Shipyard environment
    identifier: shipyard
    spec:
      # connectorRef: my_docker_connector   # needed on Kubernetes/VM infrastructure
      image: ghcr.io/shipyard/harness-plugin:1
      settings:
        api_token: <+secrets.getValue("shipyard_api_token")>
        timeout_minutes: "30"
- step:
    type: Run
    name: Run e2e tests
    identifier: e2e_tests
    spec:
      shell: Sh
      envVariables:
        CYPRESS_BASE_URL: <+steps.shipyard.output.outputVariables.SHIPYARD_ENVIRONMENT_URL>
        CYPRESS_BYPASS_TOKEN: <+steps.shipyard.output.outputVariables.SHIPYARD_BYPASS_TOKEN>
      command: npm run test
```

A full pipeline is in [`examples/e2e-pipeline.yaml`](examples/e2e-pipeline.yaml).

Either way, give the step a Harness `timeout` longer than `timeout_minutes`, so the plugin's own timeout message is what you see.

### Settings

| Setting (classic) | Env var (New Experience) | Description | Default |
| --- | --- | --- | --- |
| `api_token` | `PLUGIN_API_TOKEN` | Shipyard API token. Required. | - |
| `timeout_minutes` | `PLUGIN_TIMEOUT_MINUTES` | Minutes to wait for the environment before failing. | `60` |
| `app_name` | `PLUGIN_APP_NAME` | Only match environments of this Shipyard application. | - |
| `api_url` | `PLUGIN_API_URL` | Shipyard API base URL, for self-hosted Shipyard. | `https://shipyard.build` |
| `repo_owner`, `repo_name`, `branch` | `PLUGIN_REPO_OWNER`, `PLUGIN_REPO_NAME`, `PLUGIN_BRANCH` | Override the repo and branch taken from the Harness build. | see below |

Each setting can also come from a `SHIPYARD_*` environment variable on the step: `SHIPYARD_API_TOKEN`, `SHIPYARD_TIMEOUT`, `SHIPYARD_APP_NAME`, `SHIPYARD_API_URL`. Settings take precedence.

The repo and branch come from the build's codebase:

- **Repo**: parsed from the clone URL (`DRONE_REMOTE_URL`, then `DRONE_REPO_LINK`). `DRONE_REPO_OWNER` is only a fallback, because Harness sets it to the connector's account: with a GitHub connector owned by a user, a repo in an organization reports the user.
- **Branch**: `DRONE_SOURCE_BRANCH`, which is the PR's head branch on a PR build and the branch itself on a branch build, falling back to `DRONE_COMMIT_BRANCH`. On a PR build `DRONE_BRANCH` is the PR's *target* branch, so it isn't used.

Tag builds are rejected, since Shipyard environments are per branch. Without a codebase, set `repo_owner`, `repo_name` and `branch` yourself.

While waiting, the plugin restarts a retired (sleeping) environment once, and fails right away if the environment is stopped with no build running.

### Outputs

Read these in later steps as `<+steps.STEP_ID.output.outputVariables.NAME>`.

| Output | Description |
| --- | --- |
| `SHIPYARD_ENVIRONMENT_URL` | URL of the environment |
| `SHIPYARD_ENVIRONMENT_ID` | ID of the environment |
| `SHIPYARD_BYPASS_TOKEN` | Token to bypass authentication (output secret, masked in logs) |
| `SHIPYARD_ENVIRONMENT_COMMIT_HASH` | Commit the environment was built from, when known |
| `SHIPYARD_ENVIRONMENT_READY`, `SHIPYARD_ENVIRONMENT_RETIRED` | Environment state |
| `SHIPYARD_DOMAIN` | Main URL (same as `SHIPYARD_ENVIRONMENT_URL`) |
| `SHIPYARD_DOMAIN_<SERVICE>` | URL of each routed service, one output per service |

For apps with several routed services, there is one `SHIPYARD_DOMAIN_<SERVICE>` output per service. The name is the Compose service name uppercased, with every character other than a letter or digit replaced by `_`: service `api-gw` gives `SHIPYARD_DOMAIN_API_GW`. Services that share a host through path routes all report that host.

`SHIPYARD_BYPASS_TOKEN` is written as a Harness [output secret](https://developer.harness.io/docs/continuous-integration/use-ci/use-drone-plugins/plugin-step-settings-reference/) so it is masked in logs. Harness provides output secrets through `HARNESS_OUTPUT_SECRET_FILE`; on some accounts they need the `CI_ENABLE_OUTPUT_SECRETS` feature flag (plus `CI_ENABLE_PLUGIN_OUTPUT_SECRETS` on Docker runners), which Harness support can enable. Without them, the plugin still works but writes the token as a plain output variable and logs a warning.

Harness notes that plugin output variables can be unreliable on self-managed VM build infrastructure; Harness Cloud and Kubernetes infrastructure are supported.

## Development

```sh
make test          # pytest, against a local fake Shipyard API
make lint          # ruff
make docker.build  # build the image locally
```

Releases: push a tag like `v1.0.0` to publish `ghcr.io/shipyard/harness-plugin` with tags `1.0.0`, `1.0`, `1` and `latest`.

## Resources

[Shipyard Documentation](https://docs.shipyard.build/docs/)
