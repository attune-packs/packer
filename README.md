# HashiCorp Packer Attune Pack

This pack adapts the Apache-2.0 StackStorm Exchange `packer` pack at revision
`af813347fdf92cb24e466d4c0f57d9de73196407` for current Attune behavior and the
Packer 1.16 CLI documented in August 2026.

## Requirements

- Python 3.10 or newer and the core Python runtime.
- A current `packer` executable on every selected action worker.
- An absolute, existing, writable `ATTUNE_ARTIFACTS_DIR`.
- Network and provider access required by the selected template and plugins.

The worker may set `PACKER_EXECUTABLE` to an absolute executable path or a name
resolvable through its `PATH`; the default is `packer`. This is worker
configuration, not an action parameter.

## Actions

| Action | Behavior |
|---|---|
| `packer.init` | Installs HCL `required_plugins` beneath the execution artifact directory. |
| `packer.validate` | Validates syntax and configuration, optionally evaluating data sources. |
| `packer.inspect` | Reports template components using Packer machine-readable output. |
| `packer.build` | Builds with machine-readable output and non-interactive `-on-error=cleanup`. |
| `packer.fix` | Updates a legacy JSON template and atomically writes a mode `0600` artifact. |

Every action receives one flat JSON object on stdin and emits a structured JSON
result. Common fields include `success`, `exit_code`, `timed_out`, `command`,
`working_directory`, bounded `stdout` and `stderr`, and parsed machine-readable
`events`. A Packer nonzero exit produces the result and exits the action
entrypoint nonzero. Output is capped at 1,000,000 characters per stream.

`timeout_seconds` is bounded from 1 to 7200 seconds. The default is 900 for
build and 120 for other actions. On timeout or interruption, the action sends
SIGTERM to the Packer process group, waits up to five seconds, and then sends
SIGKILL. Resources already created by a builder or provider may still require
external cleanup.

## Paths And Artifacts

`working_directory`, `template`, `variables_file`, and `output_file` resolve
beneath `ATTUNE_ARTIFACTS_DIR`; symlinks cannot escape it. Relative template and
variable paths resolve from `working_directory`. The action creates private
Packer home, plugin, cache, and temporary directories under
`ATTUNE_ARTIFACTS_DIR/.packer` and overrides Packer's corresponding environment
settings.

This confinement controls paths accepted by the action and Packer's own local
state. Packer templates are executable infrastructure definitions: builders,
provisioners, plugins, and post-processors can access the worker, create remote
resources, or contain their own absolute output paths. Run only reviewed
templates on appropriately isolated workers. The pack is not an operating
system sandbox.

## Variables And Environment

`variables` is a JSON object. Values are serialized and delivered through
`PKR_VAR_<name>` environment entries, never through command arguments. Use
`variables_file` for a confined `.pkrvars.hcl` or `.pkrvars.json` file on build
and validate.

`environment` explicitly supplies provider, HCP, plugin-download, proxy, or
credential environment strings. The pack does not inherit arbitrary worker
environment entries. It inherits only `PATH`, locale settings, and TLS
certificate locations; provider credentials must be explicit. `HOME`, `PATH`,
`TMPDIR`, Attune variables, Packer confinement/log variables, and `PKR_VAR_*`
are reserved. Dynamic-loader, interpreter-startup, and alternate configuration
directory variables are also rejected so environment input cannot replace or
redirect the worker-selected Packer executable.

Attune marks the entire `variables` and `environment` parameters secret. Their
string values are also redacted from captured stdout and stderr. Packer or a
plugin can transform a secret before printing it, which cannot be reliably
detected, so templates and plugins must still avoid logging credentials.

Example build parameters:

```json
{
  "working_directory": "images/base",
  "template": "ubuntu.pkr.hcl",
  "variables": {"image_version": "2026.08.1"},
  "environment": {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "REDACTED"},
  "only": ["amazon-ebs.base"],
  "parallel_builds": 1,
  "timeout_seconds": 1800
}
```

## Removed Atlas Push

The source pack's `packer.push` action uploaded templates to HashiCorp Atlas and
used an `atlas_token`. Atlas push is absent from the current Packer CLI and is
not compatible with HCP Packer. This pack intentionally does not register a
`packer.push` action. Configure HCP Packer blocks and `HCP_CLIENT_ID` /
`HCP_CLIENT_SECRET` in `environment`, then use `packer.build` for current HCP
Packer workflows.

## Fidelity

| Source | Attune target | Fidelity | Important differences |
|---|---|---|---|
| `build` | `packer.build` | adapted | Current flags, structured machine output, safe argv, timeout, cleanup-only failure behavior, artifact path checks. |
| `validate` | `packer.validate` | adapted | Current HCL/data-source options, structured machine output, explicit environment. |
| `inspect` | `packer.inspect` | adapted | Current machine-readable output rather than the abandoned Python wrapper parser. |
| `fix` | `packer.fix` | adapted | Still current for legacy JSON; writes a confined private artifact without shell redirection. |
| None | `packer.init` | added | Current HCL plugin initialization with confined plugin storage. |
| Atlas `push` | no action | omitted | Atlas operation was removed; HCP Packer is configured in templates and used during build. |
| StackStorm config | worker/action inputs | adapted | No global variable or Atlas-token config; explicit per-execution secret parameters. |

## Validation

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
attune --output json pack check /home/david/Codebase/attune-packs/packer
attune pack test /home/david/Codebase/attune-packs/packer --detailed
```

Tests use a deterministic fake Packer executable and do not contact providers,
HCP, plugin registries, or other external services.

## Upstream And License

The exact upstream revision and version are recorded in `pack.yaml` and
`NOTICE`. The upstream Apache License 2.0 is included in `LICENSE`. The current
CLI behavior was checked against HashiCorp's Packer 1.16 command documentation.
