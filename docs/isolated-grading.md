# Private grading

## Native tests

Use `native-script-v1` for new schema-version 2 tasks. Keep the brief in
`drafts/<task>/control/task-brief.md`. Describe required behavior, public interfaces
and defaults, material edge cases and behavior that must remain stable. Give the
coding agent enough information to use the project’s public build and test commands.
Independent preparation review checks the brief against the held-out tests. It also
checks that the brief keeps solution details and private test contents out of the prompt.

Select a regular repository-relative `.sh` file with the other private artifacts.
The script must invoke the project's native test runner, select the applicable tests
or suite, and produce JUnit XML at `$MINEVAL_TEST_REPORT`. For a Python library with
pytest installed in the project image, a script run from the project root can contain:

```sh
#!/bin/sh
exec python -m pytest tests/test_held_out.py --junitxml="$MINEVAL_TEST_REPORT"
```

```json
{
  "grading_type": "native-script-v1",
  "image": "my-project:prepared",
  "workdir": "/workspace",
  "script": "verify.sh",
  "required_tests": ["tests.test_held_out::test_direct_library_import"]
}
```

Use the IDs emitted by the runner as `classname::name`, or just `name` for reports
without a classname. Paths in the artifact list and the script path are relative to
the workspace root. Mineval invokes the frozen file with `/bin/sh` from the configured
working directory. The script should include any build or setup steps it needs and
exit with the runner's status. Native grading defaults to an outbound-isolated network.
A pass requires the command to succeed, a readable report, every required test to
pass and no reported failure. Reports distinguish failed, skipped and missing tests
from grading setup errors such as a missing or malformed runner report.

Base validation, reference validation when available, and candidate grading each
use a fresh workspace. Mineval freezes the script, checks and required IDs after
independent review and uses their original bytes for each candidate. Native tests
and their fixtures are visible during grading, where they can import the candidate
library directly. The separate coding sandbox receives the historical project and
frozen brief.

## Project dependencies

Declare the same `services` array in diagnostic and native evaluation
configurations. Use a preloaded Docker image ID or repository digest. This example
shows the PostgreSQL settings with a placeholder image ID:

```json
{
  "name": "database",
  "image": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "environment": {"POSTGRES_PASSWORD": "fixture-password"},
  "ready": ["pg_isready", "-h", "127.0.0.1"],
  "tmpfs": ["/var/run/postgresql"],
  "limits": {"timeout_seconds": 30}
}
```

Put the object inside `services`. The project reaches it via DNS name `database`.
Use fixture credentials. Mineval uses the image's entrypoint and default command
unless `argv` overrides the command. Image-declared volumes and listed temporary
paths receive disposable memory-backed storage. Service containers and their private
network are removed when the owning sandbox finishes or is cancelled. Diagnostics,
coding, private validation and grading each start their own service group. Candidates
can build and run public developer tests against these dependencies. Coding defaults
to outbound access, configurable with `agent_network` in the diagnostic config.
Use the same project image reference, workdir and service declarations across
preparation and grading. Mineval pins the resolved project image ID from diagnostics.

## CLI and HTTP checks

`isolated-argv-v1` supports command-line programs that accept one string argument
and produce an exact UTF-8 standard output. Private selected artifacts must each
be a JSON object with a nonempty `cases` list, for example:

```json
{"cases": [{"input": "7", "expected": "14\n"}]}
```

Select the case files as private verification artifacts when preparing the task.
Use an evaluation configuration such as:

```json
{
  "image": "python:3.12-slim",
  "network": "bridge",
  "grading_type": "isolated-argv-v1",
  "commands": [{"name": "cases", "kind": "tests", "argv": ["python3", "app.py"]}]
}
```

Exactly one `tests` command is required. Mineval invokes it once for each case,
appending the input as a single argument, and compares stdout on the grader side.
Other configured setup, build, and smoke commands run in the candidate container;
they cannot access private files. Each case's input is visible to the candidate.
The expected output, case files, and other private verification files are not
mounted or copied into that container. A nonzero test command follows the existing
exit-code classifications; truncated output is an error. All case files together
must contain between 1 and 1000 cases, with each input and expected output at most
32 KiB. Each case file is limited to 1 MiB.

Use the existing `network` setting as needed. Networked candidate code can contact
network services; the operator must not expose private grading files through those
services. Configured commands and the container image are trusted inputs and must
not embed private expected results or mount them themselves.

Existing `isolated-argv-v1` and `isolated-http-v1` results remain readable.
These checks are intended for tasks that expose those interfaces.
