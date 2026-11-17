"""Compare two source-audit provenance sidecars across runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .models import SourceAuditProvenance, group_source_audit_provenances

SIDECAR_VERSION = 1
COMPARISON_VERSION = 1

# The envelope schema and the audit implementation are code identity. Provider
# version and all bounded settings are configuration identity. The response
# digest is the observed data identity.
CODE_FIELDS = ("version", "audit_logic_version")
CONFIGURATION_FIELDS = (
    "provider",
    "provider_version",
    "provider_base_url",
    "max_pages",
    "page_size",
    "max_ids",
    "max_citing_opinions",
    "max_invalidator_depth",
    "allocation_policy",
    "cache_mode",
    "cache_version",
    "cache_ttl_seconds",
)
DATA_FIELDS = ("response_digest",)


@dataclass(frozen=True)
class ProvenanceAudit:
    """One source audit read from a provenance sidecar."""

    target_case: str
    target_citation: str
    source_opinion_id: str
    source_cluster_id: str
    provenance: SourceAuditProvenance | None


@dataclass(frozen=True)
class ProvenanceSidecar:
    """The comparable portion of one provenance sidecar."""

    path: str
    run_id: str
    issue: str
    audits: list[ProvenanceAudit]


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def load_provenance_sidecar(path: str | Path) -> ProvenanceSidecar:
    """Load and validate a sidecar for comparison.

    Unsupported or malformed sidecars raise ``ValueError`` with the path and
    source-audit index so the CLI can report an actionable error.
    """
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{source}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source}: invalid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{source}: invalid JSON at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{source}: sidecar root must be a JSON object")
    version = raw.get("version")
    if version != SIDECAR_VERSION:
        raise ValueError(
            f"{source}: unsupported sidecar version {version!r}; "
            f"expected {SIDECAR_VERSION}"
        )
    audits_raw = raw.get("source_audits")
    if not isinstance(audits_raw, list):
        raise ValueError(f"{source}: source_audits must be a JSON list")

    audits: list[ProvenanceAudit] = []
    for index, item in enumerate(audits_raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"{source}: source_audits[{index}] must be a JSON object"
            )
        provenance_raw = item.get("provenance")
        provenance: SourceAuditProvenance | None = None
        if provenance_raw is not None:
            if not isinstance(provenance_raw, dict):
                raise ValueError(
                    f"{source}: source_audits[{index}].provenance "
                    "must be a JSON object or null"
                )
            try:
                provenance = SourceAuditProvenance.model_validate(
                    provenance_raw
                )
            except ValidationError as exc:
                detail = exc.errors()[0]
                location = ".".join(str(part) for part in detail.get("loc", ()))
                suffix = f".{location}" if location else ""
                raise ValueError(
                    f"{source}: source_audits[{index}].provenance"
                    f"{suffix} is invalid: {detail['msg']}"
                ) from exc
            if not provenance.is_recorded:
                provenance = None
        audits.append(
            ProvenanceAudit(
                target_case=_string(item.get("target_case")),
                target_citation=_string(item.get("target_citation")),
                source_opinion_id=_string(item.get("source_opinion_id")),
                source_cluster_id=_string(item.get("source_cluster_id")),
                provenance=provenance,
            )
        )
    return ProvenanceSidecar(
        path=str(source),
        run_id=_string(raw.get("run_id")),
        issue=_string(raw.get("issue")),
        audits=audits,
    )


def _audit_key(audit: ProvenanceAudit) -> tuple[str, str, str, str]:
    """Identify the same audit in both runs without relying on list order."""
    return (
        audit.source_opinion_id,
        audit.source_cluster_id,
        audit.target_case,
        audit.target_citation,
    )


def _audit_metadata(audit: ProvenanceAudit) -> dict[str, str]:
    return {
        "target_case": audit.target_case,
        "target_citation": audit.target_citation,
        "source_opinion_id": audit.source_opinion_id,
        "source_cluster_id": audit.source_cluster_id,
    }


def _field_differences(
    left: SourceAuditProvenance,
    right: SourceAuditProvenance,
    fields: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    differences: dict[str, dict[str, object]] = {}
    for field in fields:
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value != right_value:
            differences[field] = {"a": left_value, "b": right_value}
    return differences


def _mixed_provenance(audits: list[ProvenanceAudit]) -> bool:
    """Whether one sidecar's own audits are internally non-comparable."""
    recorded = [
        audit.provenance for audit in audits if audit.provenance is not None
    ]
    if not recorded:
        return False
    groups = group_source_audit_provenances(recorded)
    return len(groups) > 1 or len(recorded) < len(audits)


def _sidecar_summary(sidecar: ProvenanceSidecar) -> dict[str, object]:
    recorded = sum(
        1 for audit in sidecar.audits if audit.provenance is not None
    )
    return {
        "path": sidecar.path,
        "run_id": sidecar.run_id,
        "issue": sidecar.issue,
        "source_audit_count": len(sidecar.audits),
        "recorded_source_audit_count": recorded,
        "unrecorded_source_audit_count": len(sidecar.audits) - recorded,
        "mixed_provenance": _mixed_provenance(sidecar.audits),
    }


def compare_provenance_sidecars(
    sidecar_a: ProvenanceSidecar,
    sidecar_b: ProvenanceSidecar,
) -> dict[str, object]:
    """Compare two sidecars and classify every comparability difference."""
    remaining = list(sidecar_b.audits)
    pairs: list[tuple[ProvenanceAudit, ProvenanceAudit]] = []
    only_in_a: list[dict[str, str]] = []
    for audit_a in sidecar_a.audits:
        match_index = next(
            (
                index
                for index, audit_b in enumerate(remaining)
                if _audit_key(audit_a) == _audit_key(audit_b)
            ),
            None,
        )
        if match_index is None:
            only_in_a.append(_audit_metadata(audit_a))
        else:
            pairs.append((audit_a, remaining.pop(match_index)))
    only_in_b = [_audit_metadata(audit) for audit in remaining]

    configuration_drift: list[dict[str, object]] = []
    code_drift: list[dict[str, object]] = []
    code_unverified: list[dict[str, object]] = []
    data_drift: list[dict[str, object]] = []
    data_unverified: list[dict[str, object]] = []
    for audit_a, audit_b in pairs:
        if audit_a.provenance is None or audit_b.provenance is None:
            continue
        configuration = _field_differences(
            audit_a.provenance, audit_b.provenance, CONFIGURATION_FIELDS
        )
        if configuration:
            configuration_drift.append(
                {
                    **_audit_metadata(audit_a),
                    "differences": configuration,
                }
            )
        code = _field_differences(
            audit_a.provenance, audit_b.provenance, CODE_FIELDS
        )
        logic_known = bool(
            audit_a.provenance.audit_logic_version
            and audit_b.provenance.audit_logic_version
        )
        if not logic_known and "version" not in code:
            code_unverified.append(
                {**_audit_metadata(audit_a), "differences": code}
            )
        elif code:
            code_drift.append(
                {**_audit_metadata(audit_a), "differences": code}
            )
        left_digest = audit_a.provenance.response_digest
        right_digest = audit_b.provenance.response_digest
        data = _field_differences(
            audit_a.provenance, audit_b.provenance, DATA_FIELDS
        )
        if not left_digest or not right_digest:
            data_unverified.append(
                {**_audit_metadata(audit_a), "differences": data}
            )
        elif left_digest != right_digest:
            data_drift.append(
                {**_audit_metadata(audit_a), "differences": data}
            )

    unrecorded_audits = [
        {"side": "a", **_audit_metadata(audit)}
        for audit in sidecar_a.audits
        if audit.provenance is None
    ] + [
        {"side": "b", **_audit_metadata(audit)}
        for audit in sidecar_b.audits
        if audit.provenance is None
    ]

    side_a = _sidecar_summary(sidecar_a)
    side_b = _sidecar_summary(sidecar_b)
    reasons: list[str] = []
    if not sidecar_a.audits and not sidecar_b.audits:
        reasons.append("no_source_audits")
    if side_a["mixed_provenance"]:
        reasons.append("side_a_mixed_provenance")
    if side_b["mixed_provenance"]:
        reasons.append("side_b_mixed_provenance")
    if configuration_drift:
        reasons.append("configuration_drift")
    if code_drift:
        reasons.append("code_drift")
    if code_unverified:
        reasons.append("code_unverified")
    if data_drift:
        reasons.append("data_drift")
    if data_unverified:
        reasons.append("data_unverified")
    if unrecorded_audits:
        reasons.append("unrecorded_audits")
    if only_in_a:
        reasons.append("only_in_a")
    if only_in_b:
        reasons.append("only_in_b")

    comparable = not reasons
    return {
        "version": COMPARISON_VERSION,
        "status": "comparable" if comparable else "not_comparable",
        "comparable": comparable,
        "reasons": reasons,
        "side_a": side_a,
        "side_b": side_b,
        "counts": {
            "matched_source_audits": len(pairs),
            "only_in_a": len(only_in_a),
            "only_in_b": len(only_in_b),
            "unrecorded_audits": len(unrecorded_audits),
            "configuration_drift": len(configuration_drift),
            "code_drift": len(code_drift),
            "code_unverified": len(code_unverified),
            "data_drift": len(data_drift),
            "data_unverified": len(data_unverified),
        },
        "configuration_drift": configuration_drift,
        "code_drift": code_drift,
        "code_unverified": code_unverified,
        "data_drift": data_drift,
        "data_unverified": data_unverified,
        "unrecorded_audits": unrecorded_audits,
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
    }


def _display(value: object) -> str:
    if value == "":
        return "(empty)"
    return str(value)


def _audit_label(entry: dict[str, object]) -> str:
    source = entry.get("source_opinion_id") or "unknown"
    cluster = entry.get("source_cluster_id") or "unknown"
    target = entry.get("target_case") or "unknown target"
    citation = entry.get("target_citation") or "no citation"
    return f"source {source}/cluster {cluster} — {target} ({citation})"


def _entries(result: dict[str, object], key: str) -> list[dict[str, object]]:
    value = result.get(key)
    return value if isinstance(value, list) else []


def _side_line(side: dict[str, object]) -> str:
    return (
        f"{side.get('path', 'unknown')} "
        f"(run {side.get('run_id') or 'unknown'}; "
        f"issue {side.get('issue') or 'unknown'}; "
        f"{side.get('source_audit_count', 0)} source audits, "
        f"{side.get('recorded_source_audit_count', 0)} recorded)"
    )


def _render_field_differences(
    lines: list[str], differences: object
) -> None:
    if not isinstance(differences, dict):
        return
    for field, values in differences.items():
        if not isinstance(values, dict):
            continue
        lines.append(
            f"  - {field}: {_display(values.get('a'))} -> "
            f"{_display(values.get('b'))}"
        )


def _render_drift(
    lines: list[str],
    title: str,
    entries: list[dict[str, object]],
) -> None:
    if not entries:
        lines.append(f"{title}: none")
        return
    lines.append(f"{title}:")
    for entry in entries:
        lines.append(f"- {_audit_label(entry)}")
        _render_field_differences(lines, entry.get("differences"))


def _render_audit_list(
    lines: list[str], title: str, entries: list[dict[str, object]]
) -> None:
    if not entries:
        lines.append(f"{title}: none")
        return
    lines.append(f"{title}:")
    for entry in entries:
        lines.append(f"- {_audit_label(entry)}")


def render_comparison_text(result: dict[str, object]) -> str:
    """Render a comparison result for a human reviewer."""
    comparable = bool(result.get("comparable"))
    side_a_raw = result.get("side_a")
    side_b_raw = result.get("side_b")
    side_a = side_a_raw if isinstance(side_a_raw, dict) else {}
    side_b = side_b_raw if isinstance(side_b_raw, dict) else {}
    counts = result.get("counts")
    matched = counts.get("matched_source_audits", 0) if isinstance(counts, dict) else 0
    lines = [
        f"Provenance comparison: {'COMPARABLE' if comparable else 'NOT COMPARABLE'}",
        f"A: {_side_line(side_a)}",
        f"B: {_side_line(side_b)}",
        "Matched source audits: "
        f"{matched}",
    ]
    reasons = result.get("reasons")
    if isinstance(reasons, list) and reasons:
        lines.append("Reasons: " + ", ".join(str(reason) for reason in reasons))
    if side_a.get("mixed_provenance"):
        lines.append("Side A is internally mixed; no global comparison is safe.")
    if side_b.get("mixed_provenance"):
        lines.append("Side B is internally mixed; no global comparison is safe.")
    if comparable:
        lines.append(
            "All matched audits share configuration, code, and response data."
        )
    _render_drift(
        lines,
        "Configuration drift",
        _entries(result, "configuration_drift"),
    )
    _render_drift(lines, "Code drift", _entries(result, "code_drift"))
    _render_drift(
        lines,
        "Code comparison unavailable",
        _entries(result, "code_unverified"),
    )
    _render_drift(lines, "Data drift", _entries(result, "data_drift"))
    _render_drift(
        lines,
        "Data comparison unavailable",
        _entries(result, "data_unverified"),
    )
    _render_audit_list(
        lines, "Unrecorded audits", _entries(result, "unrecorded_audits")
    )
    _render_audit_list(lines, "Only in A", _entries(result, "only_in_a"))
    _render_audit_list(lines, "Only in B", _entries(result, "only_in_b"))
    return "\n".join(lines)
