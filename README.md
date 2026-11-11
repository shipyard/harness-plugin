# Shipyard Harness CI Plugin

[![GitHub License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

Use Shipyard with [Harness CI](https://www.harness.io/products/continuous-integration) to run jobs against the ephemeral environment Shipyard deploys for your branch or pull request, authenticating into it with a bypass token.

The plugin waits for the Shipyard environment of the build's repo and branch to be ready, then exports its URL and bypass token as step outputs, so later steps can run e2e tests where OAuth would normally be required. It works the same way as the Shipyard [GitHub Action](https://github.com/shipyard/github-action) and [CircleCI orb](https://github.com/shipyardbuild/circleci-orb).

## How to use

1. Create a Harness secret holding your Shipyard API token (from your organization's settings page in Shipyard), e.g. `shipyard_api_token`.
2. Add a **Plugin** step to your CI stage, then read its outputs in a later step:

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

A full pipeline is in [`examples/e2e-pipeline.yaml`](examples/e2e-pipeline.yaml). Give the Plugin step a Harness `timeout` longer than `timeout_minutes`, so the plugin's own timeout message is what you see.

### Settings

| Setting | Description | Default |
| --- | --- | --- |
| `api_token` | Shipyard API token. Required. | - |
| `timeout_minutes` | Minutes to wait for the environment before failing. | `60` |
| `app_name` | Only match environments of this Shipyard application. | - |
| `api_url` | Shipyard API base URL, for self-hosted Shipyard. | `https://shipyard.build` |
| `repo_owner`, `repo_name`, `branch` | Override the repo and branch taken from the Harness build. | see below |

Each setting can also come from an environment variable on the step: `SHIPYARD_API_TOKEN`, `SHIPYARD_TIMEOUT`, `SHIPYARD_APP_NAME`, `SHIPYARD_API_URL`. Settings take precedence.

The repo and branch come from Harness's built-in variables: `DRONE_REPO_OWNER`, `DRONE_REPO_NAME`, and `DRONE_SOURCE_BRANCH` (the PR's head branch, or the pushed branch), falling back to `DRONE_COMMIT_BRANCH`. Tag builds are rejected, since Shipyard environments are per branch.

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
| `SHIPYARD_DOMAIN`, `SHIPYARD_DOMAIN_<NAME>` | Main URL, and each additional URL by name |

`SHIPYARD_BYPASS_TOKEN` is written as a Harness [output secret](https://developer.harness.io/docs/continuous-integration/use-ci/use-drone-plugins/plugin-step-settings-reference/) so it is masked in logs. This needs the `CI_ENABLE_OUTPUT_SECRETS` feature flag on your Harness account (plus `CI_ENABLE_PLUGIN_OUTPUT_SECRETS` on Docker runners); ask Harness support to enable them. Without them, the plugin still works but writes the token as a plain output variable and logs a warning.

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
