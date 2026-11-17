"""Bounded XML parsing and versioned, intentionally conservative fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from xml.parsers import expat

MAX_BYTES = 8 * 1024 * 1024
MAX_REPORTS = 32
MAX_DEPTH = 64
MAX_ELEMENTS = 100_000
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 256 * 1024
MAX_RETAINED_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


class Budget:
    def __init__(self, limit: int, label: str):
        self.remaining = limit
        self.label = label

    def charge(self, size: int) -> None:
        if size > self.remaining:
            raise InvalidInput(f"{self.label} limit exceeded; split or reduce the reports")
        self.remaining -= size


def bounded_join(chunks, separator="") -> str:
    budget = Budget(MAX_OUTPUT_BYTES, "encoded UTF-8 output")
    result = []
    for chunk in chunks:
        if result:
            budget.charge(len(separator.encode('utf-8')))
        budget.charge(len(chunk.encode('utf-8')))
        result.append(chunk)
    return separator.join(result)


class OutputLines(list):
    def __init__(self, lines):
        super().__init__()
        self.budget = Budget(MAX_OUTPUT_BYTES, "encoded UTF-8 output")
        self += lines

    def __iadd__(self, lines):
        for line in lines:
            self.budget.charge(len(line.encode('utf-8')) + bool(self))
            self.append(line)
        return self


class InvalidInput(ValueError):
    """Malformed, unsupported or resource-limit-exceeding input."""


def local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def parse_report(path: Path, source: str, retained=None, aggregate=None) -> tuple[list[dict], dict]:
    retained = retained or Budget(MAX_RETAINED_BYTES, "retained material")
    aggregate = aggregate or Budget(MAX_TOTAL_BYTES, "aggregate input bytes")
    # Reading one extra byte detects oversize files without allocating their size.
    with path.open("rb") as stream:
        data = stream.read(min(MAX_BYTES, aggregate.remaining) + 1)
    aggregate.charge(len(data))
    if len(data) > MAX_BYTES:
        raise InvalidInput(f"{source}: exceeds {MAX_BYTES} bytes")
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[str] = []
    suites: list[str] = []
    current: dict | None = None
    diagnostic: dict | None = None
    failures: list[dict] = []
    stats = dict(tests=0, passed=0, skipped=0, failed_tests=0, failure_elements=0, error_elements=0)
    elements = 0

    def reject(*args: object) -> None:
        raise InvalidInput(f"{source}: DTDs and entity declarations are not supported")

    def start(raw: str, attrs: dict) -> None:
        nonlocal elements, current, diagnostic
        name = local(raw)
        parent = stack[-1] if stack else None
        elements += 1
        if elements > MAX_ELEMENTS or len(stack) >= MAX_DEPTH:
            raise InvalidInput(f"{source}: XML element/depth limit exceeded")
        if not stack and name not in ("testsuite", "testsuites"):
            raise InvalidInput(f"{source}: root must be testsuite or testsuites")
        if name in ("testsuite", "testsuites"):
            if parent not in (None, "testsuite", "testsuites"):
                raise InvalidInput(f"{source}: unsupported suite placement")
            suites.append(attrs.get("name", ""))
        if name == "testcase":
            if current is not None or parent not in ("testsuite", "testsuites"):
                raise InvalidInput(f"{source}: unsupported testcase placement")
            stats["tests"] += 1
            current = dict(name=attrs.get("name", ""), classname=attrs.get("classname", ""),
                           suite=[s for s in suites if s], source=source,
                           testcase_index=stats["tests"], events=[], skipped=False)
        if name in ("failure", "error", "skipped"):
            if current is None or parent != "testcase" or diagnostic is not None:
                raise InvalidInput(f"{source}: unsupported {name} placement")
            if name == "skipped":
                current["skipped"] = True
            else:
                size = sum(len(attrs.get(k, '').encode('utf-8')) for k in ('type', 'message'))
                if size > MAX_DIAGNOSTIC_BYTES:
                    raise InvalidInput(f"{source}: diagnostic exceeds {MAX_DIAGNOSTIC_BYTES} UTF-8 bytes")
                diagnostic = dict(kind=name, type=attrs.get("type", ""),
                                  message=attrs.get("message", ""), chunks=[], size=size)
        stack.append(name)

    def end(raw: str) -> None:
        nonlocal current, diagnostic
        name = local(raw)
        if name in ("failure", "error") and diagnostic is not None:
            diagnostic.pop('size')
            diagnostic["text"] = "".join(diagnostic.pop("chunks"))
            assert current is not None
            current["events"].append(diagnostic)
            diagnostic = None
        if name == "testcase":
            assert current is not None
            events = current.pop("events")
            skipped = current.pop("skipped")
            if events:
                stats["failed_tests"] += 1
                for index, event in enumerate(events, 1):
                    # Account repeated references before retaining each event.
                    size = sum(len(str(v).encode('utf-8')) for v in current.values())
                    size += sum(len(v.encode('utf-8')) for v in event.values())
                    retained.charge(size + 1024)
                    stats[event["kind"] + "_elements"] += 1
                    failures.append(dict(test=dict(current), event_index=index, **event))
            else:
                stats["skipped" if skipped else "passed"] += 1
            current = None
        if name in ("testsuite", "testsuites"):
            suites.pop()
        stack.pop()

    def chars(value: str) -> None:
        if diagnostic is not None:
            diagnostic['size'] += len(value.encode('utf-8'))
            if diagnostic['size'] > MAX_DIAGNOSTIC_BYTES:
                raise InvalidInput(f"{source}: diagnostic exceeds {MAX_DIAGNOSTIC_BYTES} UTF-8 bytes")
            diagnostic["chunks"].append(value)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise InvalidInput(f"{source}: malformed XML at line {exc.lineno}, column {exc.offset}") from exc
    return failures, stats


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Only Python traceback location syntax, not arbitrary numbers in messages.
    text = re.sub(r'(File "[^"\n]+", line )\d+\b', r'\1<LINE>', text)
    # Only object-repr addresses; preserve hex numbers that may be actual data.
    return re.sub(r'(<[^<>\n]+ at )0x[0-9a-fA-F]+(>)', r'\1<ADDRESS>\2', text)


def fingerprint(event: dict) -> tuple[str, dict]:
    basis = {key: normalize(event[key]) for key in ("kind", "type", "message", "text")}
    # Uninformative diagnostics are not evidence of a shared cause.
    if not event["message"].strip() and not event["text"].strip():
        basis["empty_diagnostic_identity"] = {**event["test"], "event_index": event["event_index"]}
    canonical = json.dumps(basis, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest(), basis


def analyze(paths: list[Path]) -> dict:
    if not paths or len(paths) > MAX_REPORTS:
        raise InvalidInput(f"expected 1–{MAX_REPORTS} input reports")
    # Full paths retain duplicate basenames and repeated test IDs across reports.
    unique = sorted({p.resolve() for p in paths}, key=lambda p: p.as_posix())
    groups: dict[str, dict] = {}
    aggregate = Budget(MAX_TOTAL_BYTES, 'aggregate input bytes')
    retained = Budget(MAX_RETAINED_BYTES, 'retained material')
    summary = dict(reports=len(unique), tests=0, passed=0, skipped=0, failed_tests=0,
                   failure_elements=0, error_elements=0)
    for path in unique:
        events, stats = parse_report(path, path.as_posix(), retained, aggregate)
        for key, count in stats.items():
            summary[key] += count
        for event in events:
            digest, basis = fingerprint(event)
            group = groups.setdefault(digest, dict(id=digest, count=0,
                fingerprint=dict(algorithm="junit-conservative-v1", normalized=basis),
                representative=event, occurrences=[]))
            group["count"] += 1
            group["occurrences"].append(event)
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["id"]))
    summary["groups"] = len(ordered)
    summary["events"] = summary["failure_elements"] + summary["error_elements"]
    return dict(schema_version="1.0", summary=summary, groups=ordered)


def render_json(report: dict) -> str:
    encoder = json.JSONEncoder(ensure_ascii=True, indent=2, sort_keys=True)
    def chunks():
        yield from encoder.iterencode(report)
        yield '\n'
    return bounded_join(chunks())


def escape(value: str) -> str:
    # Escape Markdown and HTML from untrusted test identifiers.
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = re.sub(r'([\\`*_{}\[\]()#+.!|~-])', r'\\\1', value)
    return value.replace("\r", " ").replace("\n", " ")


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = OutputLines(["# CI Log Triage", "", f"Reports: {s['reports']} | Tests: {s['tests']} | "
             f"Failed tests: {s['failed_tests']} | Failure/error events: {s['events']} | "
             f"Groups: {s['groups']}", "", f"Passed: {s['passed']} | Skipped: {s['skipped']}", ""])
    if not report["groups"]:
        lines += ["No failure or error elements found.", ""]
    for group in report["groups"]:
        event = group["representative"]
        lines += [f"## {group['count']} occurrence(s): {escape(event['type'] or event['kind'])}", "",
                  f"Fingerprint: `{group['id']}` (junit-conservative-v1)", "",
                  "Representative message: " + escape(event["message"]), ""]
        # A fence longer than any input backtick run cannot be closed by input.
        text = event["text"]
        fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r'`+', text)), default=0))
        lines += [fence + "text", text, fence, "", "Affected tests:", ""]
        for occurrence in group["occurrences"]:
            test = occurrence["test"]
            label = " / ".join(test["suite"] + [test["classname"], test["name"]])
            lines += [f"- {escape(label)} — {escape(test['source'])} "
                      f"(test #{test['testcase_index']}, event #{occurrence['event_index']})"]
        lines += ["", "Inspect these diagnostics and affected tests; grouping is textual, not proof of a shared root cause.", ""]
    return "\n".join(lines)
