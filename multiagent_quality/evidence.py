"""Verify trusted runner attestations and current artifact bytes.

HMAC is an integrity boundary, not a substitute for an isolated trusted runner.
Untrusted agents must not receive its key or alter its code/configuration.
"""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from .validation import ContractError, obj, text, sequence, unique_strings, verdict

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")

def check_key(key):
    if not isinstance(key, bytes) or len(key) < 32:
        raise ContractError("trusted runner key must contain at least 32 bytes")

def sign_report(report, key):
    check_key(key)
    return {"report": report, "signature": hmac.new(key, canonical(report), hashlib.sha256).hexdigest()}

def artifact_path(root, relative):
    text(relative, "artifact path")
    if "\\" in relative or Path(relative).is_absolute() or ":" in relative:
        raise ContractError("artifact paths must be relative POSIX paths")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise ContractError("artifact paths cannot traverse directories")
    base = Path(root).resolve(strict=True)
    path = (base / relative).resolve(strict=True)
    if not path.is_relative_to(base) or not path.is_file():
        raise ContractError("artifact must be a regular file under artifact root")
    return path

def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
    return h.hexdigest()

def utc_time(value):
    text(value, "timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ContractError("timestamp must have timezone")
    return dt.astimezone(timezone.utc)

def check_evidence(envelope, *, key, expected_context, required_test_ids,
                   artifact_root, required_artifacts, now=None, max_age_seconds=3600):
    check_key(key)
    obj(expected_context, "expected_context")
    if not expected_context:
        raise ContractError("empty expected_context is forbidden")
    required = set(unique_strings(required_test_ids, "required_test_ids"))
    required_files = set(unique_strings(required_artifacts, "required_artifacts", nonempty=False))
    if type(max_age_seconds) is not int or max_age_seconds < 1:
        raise ContractError("max_age_seconds must be a positive integer")
    current = now or datetime.now(timezone.utc)
    if current.utcoffset() is None:
        raise ContractError("now must have timezone")
    try:
        obj(envelope, "envelope")
        report = obj(envelope.get("report"), "report")
        signature = text(envelope.get("signature"), "signature")
        expected_sig = hmac.new(key, canonical(report), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return verdict(False, ["untrusted or tampered runner report"])
        reasons = []
        if report.get("schema_version") != 1 or type(report.get("schema_version")) is not int:
            reasons.append("unsupported evidence schema")
        if canonical(report.get("context")) != canonical(expected_context):
            reasons.append("stale or mismatched task/input/commit context")
        started = utc_time(report.get("started_at")); finished = utc_time(report.get("finished_at"))
        if started > finished or finished > current or (current - finished).total_seconds() > max_age_seconds:
            reasons.append("invalid or expired report timestamps")
        cases = sequence(report.get("cases"), "cases"); seen = set()
        for case in cases:
            obj(case, "case")
            cid = text(case.get("test_id"), "test_id")
            if cid in seen: reasons.append(f"duplicate test evidence: {cid}")
            seen.add(cid)
            if case.get("status") != "PASS" or type(case.get("exit_code")) is not int or case["exit_code"] != 0:
                reasons.append(f"test did not pass: {cid}")
            log_path = artifact_path(artifact_root, case.get("log_path"))
            if sha256_file(log_path) != case.get("log_sha256"):
                reasons.append(f"changed test log: {cid}")
        reasons.extend(f"missing prescribed evidence: {x}" for x in sorted(required - seen))
        listed = set()
        for item in sequence(report.get("artifacts"), "artifacts"):
            obj(item, "artifact"); name = text(item.get("path"), "artifact path")
            if name in listed: reasons.append(f"duplicate artifact: {name}")
            listed.add(name)
            path = artifact_path(artifact_root, name)
            if sha256_file(path) != item.get("sha256"):
                reasons.append(f"changed artifact: {name}")
        reasons.extend(f"missing artifact: {x}" for x in sorted(required_files - listed))
        return verdict(not reasons, reasons, context=expected_context,
                       report_sha256=hashlib.sha256(canonical(report)).hexdigest())
    except (ContractError, ValueError, TypeError, OSError, RecursionError) as exc:
        return verdict(False, [f"invalid evidence: {exc}"])
