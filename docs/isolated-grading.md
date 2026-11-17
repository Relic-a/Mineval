# Isolated private grading

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

Existing in-process tests, fixtures, and arbitrary shell test commands are not
supported for isolated grading. They return an `unsupported_grading_type` error
instead of running with private files in the candidate environment. Optional AI
assessment and adaptation are unavailable here because their previous environment
combined candidate code and private files. This contract does not establish that
arbitrary pre-existing assertions executed; false-positive grading remains a
separate issue.
