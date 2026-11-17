# CI Log Triage

CI Log Triage 0.1 groups failure and error elements from local JUnit XML into
deterministic JSON or Markdown reports. No runtime dependencies, network calls,
services, or AI models. Python 3.10 or later is required.

It is a diagnostic index, not root-cause analysis: matching fingerprints do not
prove semantic equivalence. It does not parse arbitrary console logs, fetch CI
artifacts, recommend code fixes, or decide whether a CI job should pass.

## Install and use

From this source directory, in your own virtual environment:

```console
python -m pip install .
ci-log-triage --help
ci-log-triage reports/junit.xml
ci-log-triage reports/one.xml reports/two.xml --format json
ci-log-triage "reports/*.xml" --format markdown --output triage.md
```

Installation needs setuptools >=77 (normally supplied by pip's build isolation).
Runtime is fully offline. With build tooling already installed, an offline
installation can use `pip install --no-build-isolation --no-deps .`.
JSON to stdout is the default. Patterns are expanded even on shells that do not
expand globs; at most 32 matches are accepted. Repeated resolved paths are read
once. Output files replace an existing output atomically after successful input
validation. Output cannot be any input file, including symlink/hardlink aliases.
Input/output paths must be trusted local filesystem locations, not concurrently
modified by an attacker.

## Example

```xml
<testsuite name="unit">
  <testcase classname="test_api" name="test_response">
    <failure type="AssertionError" message="expected 200">received 503</failure>
  </testcase>
</testsuite>
```

The Markdown summary begins:

```text
# CI Log Triage

Reports: 1 | Tests: 1 | Failed tests: 1 | Failure/error events: 1 | Groups: 1

Passed: 0 | Skipped: 0
```

It then lists a representative diagnostic, fingerprint and affected tests with
source paths. Inspect the most frequent groups first and use those references
to investigate; the tool does not infer a cause or suggest an unverified fix.

## Fingerprint contract: junit-conservative-v1

The grouping key contains classification (`failure` versus `error`), type,
message and diagnostic text. Normalize CRLF/CR to LF; replace numbers only in
Python traceback `File "...", line 123` locations; replace hex addresses only
inside object reprs like `<Thing at 0xABC>`. Preserve paths (including temporary
paths), arbitrary numeric values, indentation and exception messages. Temporary
directory names may be meaningful: v0.1 intentionally does not normalize them.

Serialize those fields as ASCII-escaped, sorted-key, compact JSON and hash with
SHA-256. Full digest and normalized fields are included in JSON. Empty message
and diagnostic pairs additionally include source/test/event identity, rather
than grouping uninformative failures by exception type alone. SHA collisions
are a theoretical limitation. Normalization can both miss related failures and
merge different causes; inspect original occurrences before drawing conclusions.

Reports are processed in sorted resolved-path order, events in XML order;
groups sort by descending event count then full digest. No timestamps or random
IDs enter output. Paths are absolute POSIX-style strings, so moving reports to
another directory changes output. All original messages and diagnostic text
are retained in JSON (subject to XML's standard newline/entity decoding).

## JSON schema 1.0

Top level: `schema_version` (string), `summary` (integer counters), `groups`
(array). Summary counts actual testcase elements, not potentially stale suite
attributes: reports, tests, passed, skipped, failed_tests, failure_elements,
error_elements, events, groups. Each group has `id`, `count`, `fingerprint`
(`algorithm`, `normalized`), `representative`, and `occurrences`.
Each occurrence has `kind`, `type`, `message`, `text`, `event_index`, and `test`.
Test references contain `source`, `suite` (ancestor names), `classname`, `name`,
and one-based report-local `testcase_index`. Duplicate identifiers remain
distinct occurrences. A testcase can contribute several failure/error events.
Failures take precedence over a skipped marker on the same testcase.

## Security and format limits

XML is parsed with standard-library Expat, with all DTDs and entity declarations
rejected before expansion, including UTF-16 input. No external entity fetches.
Predefined XML escapes and namespaces are supported. Keep Python/Expat patched.
Fixed resource limits (not configurable): 8 MiB per file; 16 MiB aggregate raw
input bytes after path deduplication; 32 reports; 64 XML element levels;
100,000 elements per report. Each diagnostic (type + message + text) is limited
to 256 KiB of decoded UTF-8. Retained events have a shared 4 MiB accounting
budget including test references and a conservative 1024-byte charge per event.
JSON and Markdown are limited to 16 MiB of encoded UTF-8 output, counted
incrementally before final joining. These limits accommodate small CI reports
while bounding multiplication of diagnostic evidence. Oversize reports fail
with exit 2 and an explicit limit error: split or reduce inputs; no truncation,
partial stdout or replacement of an existing output on resource rejection.
Whole bounded XML files, parser tokens, Python objects and serialization chunks
still use memory; accounting is not a strict process-RAM ceiling or protection
against every denial-of-service input. Previously accepted oversized reports
may now be rejected; supported fingerprints and JSON structure are unchanged.

Root must be testsuite/testsuites. Nested suites and direct testcase children
are supported. Failure/error/skipped markers must be direct testcase children;
unexpected placements are rejected rather than silently dropped. Unknown
metadata is ignored; suite counts, attachments, logs outside failure/error,
retry-specific vendor elements and compressed files are not interpreted.
Zero-byte files are invalid; `<testsuites/>` is an empty valid report.
Markdown escapes identifiers and fences diagnostic text; treat output as
untrusted data. Diagnostics and absolute paths may contain secrets: review
before sharing; this is not a redaction tool.

## Exit codes

- 0: report generated, even when tests failed; also `--help`.
- 2: invalid CLI/input, unmatched glob, missing input, malformed/unsupported XML,
  resource limit or attempt to overwrite input.
- 3: operational problem, such as permissions, output I/O or encoding failure.

Errors go to stderr. No partial report is emitted when an input fails.

## Local development

```console
python -m venv .venv
# Activate the venv using your shell, then:
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

Tests use unittest and synthetic fixtures, without a Qt installation, pytest,
network, or third-party test dependencies. CI runs installation, unittest and
CLI smoke checks on Python 3.10 and 3.13 across Linux and Windows. The workflow
is provided as source; local development does not trigger remote CI.
