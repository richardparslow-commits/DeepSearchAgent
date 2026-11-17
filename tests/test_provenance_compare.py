"""Focused tests for cross-run provenance sidecar comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from va_legal_agent.__main__ import main
from va_legal_agent.models import (
    SOURCE_AUDIT_LOGIC_VERSION,
    SourceAuditProvenance,
)
from va_legal_agent.provenance_compare import (
    SIDECAR_VERSION,
    compare_provenance_sidecars,
    load_provenance_sidecar,
    render_comparison_text,
)


def _provenance(**overrides: object) -> SourceAuditProvenance:
    values: dict[str, object] = {
        "source_opinion_id": "201",
        "source_cluster_id": "888",
        "provider": "courtlistener",
        "provider_version": "v4",
        "provider_base_url": "https://www.courtlistener.com/api/rest/v4/",
        "audit_logic_version": SOURCE_AUDIT_LOGIC_VERSION,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "max_pages": 1,
        "page_size": 12,
        "max_ids": 12,
        "max_citing_opinions": 3,
        "max_invalidator_depth": 1,
        "allocation_policy": "equal",
        "cache_mode": "disabled",
        "cache_version": 1,
        "cache_ttl_seconds": 0,
        "response_digest": "digest-a",
    }
    values.update(overrides)
    return SourceAuditProvenance(**values)


def _audit(
    provenance: SourceAuditProvenance | None,
    *,
    source_opinion_id: str = "201",
    source_cluster_id: str = "888",
    target_case: str = "Smith v. Wilkie",
    target_citation: str = "30 Vet.App. 123",
) -> dict[str, object]:
    return {
        "target_case": target_case,
        "target_citation": target_citation,
        "source_opinion_id": source_opinion_id,
        "source_cluster_id": source_cluster_id,
        "provenance_recorded": provenance is not None,
        "provenance": (
            provenance.model_dump(mode="json")
            if provenance is not None
            else None
        ),
    }


def _write_sidecar(
    path: Path,
    audits: list[dict[str, object]],
    *,
    run_id: str = "run-a",
    issue: str = "service connection",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": SIDECAR_VERSION,
                "run_id": run_id,
                "issue": issue,
                "source_audits": audits,
            }
        ),
        encoding="utf-8",
    )
    return path


def _sidecars(tmp_path: Path) -> tuple[Path, Path]:
    first = _write_sidecar(
        tmp_path / "a.provenance.json",
        [_audit(_provenance())],
        run_id="run-a",
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json",
        [_audit(_provenance())],
        run_id="run-b",
    )
    return first, second


def test_identical_sidecars_are_comparable(tmp_path):
    first, second = _sidecars(tmp_path)

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["status"] == "comparable"
    assert result["comparable"] is True
    assert result["reasons"] == []
    assert result["counts"] == {
        "matched_source_audits": 1,
        "only_in_a": 0,
        "only_in_b": 0,
        "unrecorded_audits": 0,
        "configuration_drift": 0,
        "code_drift": 0,
        "code_unverified": 0,
        "data_drift": 0,
        "data_unverified": 0,
    }
    assert result["configuration_drift"] == []
    assert result["code_drift"] == []
    assert result["data_drift"] == []
    rendered = render_comparison_text(result)
    assert "Provenance comparison: COMPARABLE" in rendered
    assert "Configuration drift: none" in rendered
    assert "All matched audits share" in rendered


def test_comparison_separates_configuration_and_code_drift(tmp_path):
    first = _write_sidecar(
        tmp_path / "a.provenance.json",
        [_audit(_provenance())],
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json",
        [
            _audit(
                _provenance(
                    version=2,
                    provider_base_url="https://example.test/api/rest/v3/",
                    max_pages=2,
                    audit_logic_version="2",
                )
            )
        ],
    )

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["comparable"] is False
    assert result["status"] == "not_comparable"
    assert result["reasons"] == ["configuration_drift", "code_drift"]
    assert result["counts"]["configuration_drift"] == 1
    assert result["counts"]["code_drift"] == 1
    configuration = result["configuration_drift"][0]["differences"]
    code = result["code_drift"][0]["differences"]
    assert set(configuration) == {"provider_base_url", "max_pages"}
    assert set(code) == {"version", "audit_logic_version"}
    assert configuration["max_pages"] == {"a": 1, "b": 2}
    rendered = render_comparison_text(result)
    assert "Configuration drift:" in rendered
    assert "Code drift:" in rendered
    assert "provider_base_url: https://www.courtlistener.com" in rendered


def test_comparison_reports_unverified_code(tmp_path):
    first = _write_sidecar(
        tmp_path / "a.provenance.json",
        [_audit(_provenance(audit_logic_version=""))],
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json",
        [_audit(_provenance(audit_logic_version=""))],
    )

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["counts"]["code_drift"] == 0
    assert result["counts"]["code_unverified"] == 1
    assert result["reasons"] == ["code_unverified"]
    rendered = render_comparison_text(result)
    assert "Code comparison unavailable:" in rendered


def test_schema_drift_is_code_drift_without_logic_version(tmp_path):
    first = _write_sidecar(
        tmp_path / "a.provenance.json",
        [_audit(_provenance(version=1, audit_logic_version=""))],
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json",
        [_audit(_provenance(version=2, audit_logic_version=""))],
    )

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["counts"]["code_drift"] == 1
    assert result["counts"]["code_unverified"] == 0
    assert result["reasons"] == ["code_drift"]


def test_comparison_reports_data_drift_and_unverified_data(tmp_path):
    first = _write_sidecar(
        tmp_path / "a.provenance.json",
        [
            _audit(_provenance()),
            _audit(
                _provenance(
                    source_opinion_id="202",
                    source_cluster_id="999",
                    response_digest="digest-b",
                ),
                source_opinion_id="202",
                source_cluster_id="999",
                target_case="Second v. Wilkie",
            ),
        ],
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json",
        [
            _audit(_provenance(response_digest="digest-c")),
            _audit(
                _provenance(
                    source_opinion_id="202",
                    source_cluster_id="999",
                    response_digest="",
                ),
                source_opinion_id="202",
                source_cluster_id="999",
                target_case="Second v. Wilkie",
            ),
        ],
    )

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["counts"]["data_drift"] == 1
    assert result["counts"]["data_unverified"] == 1
    assert "data_drift" in result["reasons"]
    assert "data_unverified" in result["reasons"]
    assert result["data_drift"][0]["differences"]["response_digest"] == {
        "a": "digest-a",
        "b": "digest-c",
    }
    assert result["data_unverified"][0]["differences"]["response_digest"] == {
        "a": "digest-b",
        "b": "",
    }
    rendered = render_comparison_text(result)
    assert "Data drift:" in rendered
    assert "Data comparison unavailable:" in rendered
    assert "digest-a -> digest-c" in rendered
    assert "(empty)" in rendered


def test_comparison_reports_unrecorded_and_missing_audits(tmp_path):
    first = _write_sidecar(
        tmp_path / "a.provenance.json",
        [
            _audit(_provenance()),
            _audit(
                None,
                source_opinion_id="202",
                source_cluster_id="999",
                target_case="Legacy v. Wilkie",
            ),
        ],
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json",
        [
            _audit(_provenance()),
            _audit(
                _provenance(
                    source_opinion_id="303",
                    source_cluster_id="777",
                ),
                source_opinion_id="303",
                source_cluster_id="777",
                target_case="New v. Wilkie",
            ),
        ],
    )

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["counts"]["unrecorded_audits"] == 1
    assert result["counts"]["only_in_a"] == 1
    assert result["counts"]["only_in_b"] == 1
    assert result["unrecorded_audits"][0]["side"] == "a"
    assert result["only_in_a"][0]["source_opinion_id"] == "202"
    assert result["only_in_b"][0]["source_opinion_id"] == "303"
    assert "side_a_mixed_provenance" in result["reasons"]
    assert "unrecorded_audits" in result["reasons"]
    assert "only_in_a" in result["reasons"]
    assert "only_in_b" in result["reasons"]
    rendered = render_comparison_text(result)
    assert "Side A is internally mixed" in rendered
    assert "Unrecorded audits:" in rendered
    assert "Only in A:" in rendered
    assert "Only in B:" in rendered


def test_comparison_rejects_internally_mixed_sidecars_even_when_groups_match(
    tmp_path,
):
    first = _write_sidecar(
        tmp_path / "a.provenance.json",
        [
            _audit(_provenance()),
            _audit(
                _provenance(
                    source_opinion_id="202",
                    source_cluster_id="999",
                    max_pages=2,
                ),
                source_opinion_id="202",
                source_cluster_id="999",
                target_case="Second v. Wilkie",
            ),
        ],
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json",
        [
            _audit(_provenance()),
            _audit(
                _provenance(
                    source_opinion_id="202",
                    source_cluster_id="999",
                    max_pages=2,
                ),
                source_opinion_id="202",
                source_cluster_id="999",
                target_case="Second v. Wilkie",
            ),
        ],
    )

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["comparable"] is False
    assert result["counts"]["configuration_drift"] == 0
    assert result["reasons"] == [
        "side_a_mixed_provenance",
        "side_b_mixed_provenance",
    ]
    rendered = render_comparison_text(result)
    assert "Side A is internally mixed" in rendered
    assert "Side B is internally mixed" in rendered


def test_load_treats_placeholder_envelope_as_unrecorded(tmp_path):
    path = _write_sidecar(
        tmp_path / "placeholder.provenance.json",
        [
            {
                "target_case": "Smith v. Wilkie",
                "source_opinion_id": "201",
                "provenance": {},
            }
        ],
    )

    sidecar = load_provenance_sidecar(path)

    assert sidecar.audits[0].provenance is None


def test_comparison_handles_matched_unrecorded_audits(tmp_path):
    first = _write_sidecar(
        tmp_path / "a.provenance.json", [_audit(None)]
    )
    second = _write_sidecar(
        tmp_path / "b.provenance.json", [_audit(None)]
    )

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["counts"]["matched_source_audits"] == 1
    assert result["counts"]["unrecorded_audits"] == 2
    assert result["counts"]["configuration_drift"] == 0
    assert result["counts"]["code_drift"] == 0
    assert result["counts"]["data_drift"] == 0
    assert result["reasons"] == ["unrecorded_audits"]


def test_render_ignores_malformed_difference_entries(tmp_path):
    first, second = _sidecars(tmp_path)
    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )
    result["configuration_drift"] = [
        {"source_opinion_id": "201", "differences": "not a mapping"},
        {
            "source_opinion_id": "202",
            "differences": {"provider": "not a mapping"},
        },
    ]

    rendered = render_comparison_text(result)

    assert "Configuration drift:" in rendered
    assert "provider:" not in rendered


def test_comparison_without_source_audits_is_not_comparable(tmp_path):
    first = _write_sidecar(tmp_path / "a.provenance.json", [])
    second = _write_sidecar(tmp_path / "b.provenance.json", [])

    result = compare_provenance_sidecars(
        load_provenance_sidecar(first),
        load_provenance_sidecar(second),
    )

    assert result["comparable"] is False
    assert result["reasons"] == ["no_source_audits"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "root must be a JSON object"),
        ({"version": 2, "source_audits": []}, "unsupported sidecar version"),
        ({"version": 1, "source_audits": {}}, "must be a JSON list"),
        ({"version": 1, "source_audits": [1]}, "must be a JSON object"),
        (
            {"version": 1, "source_audits": [{"provenance": []}]},
            "must be a JSON object or null",
        ),
        (
            {
                "version": 1,
                "source_audits": [
                    {"provenance": {"allocation_policy": "invalid"}}
                ],
            },
            "allocation_policy is invalid",
        ),
    ],
)
def test_load_rejects_malformed_sidecars(tmp_path, payload, message):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_provenance_sidecar(path)


def test_load_reports_invalid_json_utf8_and_missing_file(tmp_path):
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_provenance_sidecar(invalid_json)

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="invalid UTF-8"):
        load_provenance_sidecar(invalid_utf8)

    with pytest.raises(ValueError, match="No such file"):
        load_provenance_sidecar(tmp_path / "missing.json")


def test_cli_compares_provenance_as_text_without_analysis(
    capsys, tmp_path, monkeypatch
):
    first, second = _sidecars(tmp_path)
    monkeypatch.setattr(
        "va_legal_agent.__main__.validate_search_providers",
        lambda: pytest.fail("comparison must not validate search providers"),
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda *args, **kwargs: pytest.fail("comparison must not analyze"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(second),
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "Provenance comparison: COMPARABLE" in output
    assert "Matched source audits: 1" in output


def test_cli_compares_provenance_as_json_file(
    capsys, tmp_path, monkeypatch
):
    first, second = _sidecars(tmp_path)
    output_path = tmp_path / "comparison.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(second),
            "--comparison-format",
            "json",
            "--output-file",
            str(output_path),
        ],
    )

    main()

    assert capsys.readouterr().out == ""
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "comparable"
    assert result["counts"]["matched_source_audits"] == 1


def test_cli_compare_reports_invalid_sidecar(capsys, tmp_path, monkeypatch):
    first, second = _sidecars(tmp_path)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(invalid),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "could not compare provenance sidecars" in error
    assert "invalid JSON" in error


def test_cli_compare_reports_output_file_error(
    capsys, tmp_path, monkeypatch
):
    first, second = _sidecars(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(second),
            "--output-file",
            str(tmp_path / "missing" / "comparison.json"),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "could not write comparison output" in capsys.readouterr().err


def test_cli_compare_rejects_overwriting_an_input_sidecar(
    capsys, tmp_path, monkeypatch
):
    first, second = _sidecars(tmp_path)
    original = first.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(second),
            "--output-file",
            str(first),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "must differ from both provenance sidecars" in (
        capsys.readouterr().err
    )
    assert first.read_text(encoding="utf-8") == original


def test_cli_compare_rejects_incompatible_modes(
    capsys, tmp_path, monkeypatch
):
    first, second = _sidecars(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(second),
            "--provenance-sidecar",
            str(tmp_path / "sidecar.json"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "cannot be combined with --provenance-sidecar" in (
        capsys.readouterr().err
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(second),
            "--show-config",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "cannot be combined with --show-config" in capsys.readouterr().err

    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--compare-provenance",
            str(first),
            str(second),
            "--provenance-sidecar",
            "off",
        ],
    )
    main()
    assert "Provenance comparison: COMPARABLE" in capsys.readouterr().out
