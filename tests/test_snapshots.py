"""Tests for durable evidence and provider-response snapshots."""

from __future__ import annotations

import json

import pytest

from va_legal_agent.models import (
    CaseAbstract,
    CitatorCheck,
    LegalAnalysis,
    ProvisionImpact,
    SurfacedCaseRecord,
)
from va_legal_agent.snapshots import (
    SnapshotStore,
    SnapshottingCourtListenerProvider,
    _normalize_source_url,
)


def _manifest(store: SnapshotStore) -> list[dict[str, object]]:
    if not store.manifest.exists():
        return []
    return [
        json.loads(line)
        for line in store.manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_normalize_source_url_canonicalizes_identity_fields():
    assert _normalize_source_url("HTTPS://Example.TEST:443/opinion/1/#") == (
        "https://example.test/opinion/1"
    )
    assert _normalize_source_url("http://Example.TEST:80/opinion/1/") == (
        "http://example.test/opinion/1"
    )
    assert _normalize_source_url(
        "https://example.test/opinion/1/?a=1&b=2"
    ) == "https://example.test/opinion/1?a=1&b=2"
    assert _normalize_source_url("https://example.test:8443/opinion/1/") == (
        "https://example.test:8443/opinion/1"
    )
    assert _normalize_source_url(None) == ""


def test_record_keeps_distinct_source_urls_for_same_payload(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    official = store.record(
        "courtlistener.test",
        {"a": 1},
        source_url="https://uscourts.cavc.gov/opinions/smith",
    )
    secondary = store.record(
        "courtlistener.test",
        {"a": 1},
        source_url="https://lawfirm.example/smith-summary",
    )

    assert official.id != secondary.id
    rows = _manifest(store)
    assert len(rows) == 2
    assert {row["source_url"] for row in rows} == {
        "https://uscourts.cavc.gov/opinions/smith",
        "https://lawfirm.example/smith-summary",
    }


def test_record_deduplicates_canonical_url_variants(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.record(
        "courtlistener.test",
        {"a": 1},
        source_url="HTTPS://Example.TEST:443/opinion/1/#",
    )
    second = store.record(
        "courtlistener.test",
        {"a": 1},
        source_url="https://example.test/opinion/1",
    )

    assert first.id == second.id
    assert len(_manifest(store)) == 1


def test_record_writes_content_addressed_payload_and_deduplicates(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.record(
        "courtlistener.test",
        {"b": 2, "a": 1},
        source_url="https://example.test/opinion/1/",
        metadata={"target": "x"},
    )
    second = store.record(
        "courtlistener.test",
        {"a": 1, "b": 2},
        source_url="https://example.test/opinion/1/",
        metadata={"target": "x"},
    )

    assert first.id == second.id
    assert first.path == second.path
    rows = _manifest(store)
    assert len(rows) == 1
    assert rows[0]["id"] == first.id
    assert rows[0]["source_url"] == "https://example.test/opinion/1/"
    payload = json.loads((store.root / first.path).read_text(encoding="utf-8"))
    assert payload["payload"] == {"a": 1, "b": 2}
    assert payload["metadata"] == {"target": "x"}
    assert payload["sha256"] == first.sha256


def test_record_evidence_persists_normalized_check(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    check = CitatorCheck(
        target_case="Smith v. Wilkie",
        target_url="https://example.test/opinion/1/",
        status="unknown",
    )

    record = store.record_evidence(check, source="citator-test")

    payload = json.loads((store.root / record.path).read_text(encoding="utf-8"))
    assert payload["payload"]["check"]["target_case"] == "Smith v. Wilkie"
    assert record.metadata["source"] == "citator-test"
    assert record.source_url == "https://example.test/opinion/1/"


def test_record_response_persists_request_and_response(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")

    record = store.record_response(
        "courtlistener.lookup", {"citation_text": "1 Vet.App. 1"}, {"results": []}
    )

    payload = json.loads((store.root / record.path).read_text(encoding="utf-8"))
    assert payload["payload"] == {
        "request": {"citation_text": "1 Vet.App. 1"},
        "response": {"results": []},
    }


def test_record_rejects_unsafe_kind(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")

    with pytest.raises(ValueError, match="safe characters"):
        store.record("..", {})


def test_atomic_write_cleans_temp_file_after_failure(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path / "snapshots")

    def fail_dump(*args, **kwargs):
        raise RuntimeError("serialization failed")

    monkeypatch.setattr("va_legal_agent.snapshots.json.dump", fail_dump)

    with pytest.raises(RuntimeError, match="serialization failed"):
        store.record("courtlistener.test", {"large": "payload"})

    assert list((tmp_path / "snapshots").rglob("*.tmp")) == []


class FakeProvider:
    def __init__(self):
        self.calls: list[str] = []

    def lookup_citations(self, text):
        self.calls.append("lookup")
        return {"text": text}

    def citing_edges(self, opinion_id, page_size=100, cursor=None):
        self.calls.append("edges")
        return {"opinion_id": opinion_id, "page_size": page_size, "cursor": cursor}

    def same_docket_decisions(self, opinion_id, max_results=5):
        self.calls.append("same_docket")
        return {"opinion_id": opinion_id, "max_results": max_results}

    def opinion_metadata(self, opinion_id):
        self.calls.append("metadata")
        return {"id": opinion_id, "url": f"https://example.test/opinion/{opinion_id}/"}

    def opinion_metadata_batch(self, opinion_ids):
        self.calls.append("metadata_batch")
        return {
            opinion_id: {
                "id": opinion_id,
                "url": f"https://example.test/opinion/{opinion_id}/",
            }
            for opinion_id in opinion_ids
        }

    def fetch_opinion_text(self, opinion_id):
        self.calls.append("text")
        return f"text {opinion_id}"


def test_snapshotting_provider_captures_every_public_response(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    wrapper = SnapshottingCourtListenerProvider(FakeProvider(), store)

    assert wrapper.lookup_citations("1 Vet.App. 1") == {"text": "1 Vet.App. 1"}
    assert wrapper.citing_edges(7, page_size=25, cursor="abc")["cursor"] == "abc"
    assert wrapper.same_docket_decisions(7, max_results=2)["max_results"] == 2
    assert wrapper.opinion_metadata(7)["url"].endswith("/7/")
    assert wrapper.opinion_metadata_batch([7, 8])[8]["url"].endswith("/8/")
    assert wrapper.fetch_opinion_text(7) == "text 7"

    kinds = [row["kind"] for row in _manifest(store)]
    assert kinds.count("courtlistener.lookup") == 1
    assert kinds.count("courtlistener.citing_edges") == 1
    assert kinds.count("courtlistener.same_docket") == 1
    assert kinds.count("courtlistener.opinion_metadata") == 1
    assert kinds.count("courtlistener.opinion_metadata_batch") == 1
    assert kinds.count("courtlistener.opinion_text") == 1
    metadata_row = next(
        row for row in _manifest(store) if row["kind"] == "courtlistener.opinion_metadata"
    )
    assert metadata_row["source_url"] == "https://example.test/opinion/7/"


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("lookup_citations", ("text",)),
        ("citing_edges", (7,)),
        ("same_docket_decisions", (7,)),
        ("opinion_metadata", (7,)),
        ("opinion_metadata_batch", ([7],)),
        ("fetch_opinion_text", (7,)),
    ],
)
def test_snapshotting_provider_persists_and_reraises_errors(
    tmp_path, method, args
):
    store = SnapshotStore(tmp_path / "snapshots")

    class FailingProvider(FakeProvider):
        def lookup_citations(self, text):
            raise RuntimeError("lookup failed")

        def citing_edges(self, opinion_id, page_size=100, cursor=None):
            raise RuntimeError("edges failed")

        def same_docket_decisions(self, opinion_id, max_results=5):
            raise RuntimeError("docket failed")

        def opinion_metadata(self, opinion_id):
            raise RuntimeError("metadata failed")

        def opinion_metadata_batch(self, opinion_ids):
            raise RuntimeError("batch metadata failed")

        def fetch_opinion_text(self, opinion_id):
            raise RuntimeError("text failed")

    wrapper = SnapshottingCourtListenerProvider(FailingProvider(), store)

    with pytest.raises(RuntimeError):
        getattr(wrapper, method)(*args)

    rows = _manifest(store)
    assert len(rows) == 1
    payload = json.loads((store.root / rows[0]["path"]).read_text(encoding="utf-8"))
    assert payload["payload"]["response"]["error"]


def test_snapshotting_provider_falls_back_when_native_batch_is_absent(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")

    class LegacyProvider(FakeProvider):
        opinion_metadata_batch = None

    wrapper = SnapshottingCourtListenerProvider(LegacyProvider(), store)

    response = wrapper.opinion_metadata_batch([7, 8])

    assert set(response) == {7, 8}
    assert response[7]["url"].endswith("/7/")
    kinds = [row["kind"] for row in _manifest(store)]
    assert kinds.count("courtlistener.opinion_metadata_batch") == 1
    assert kinds.count("courtlistener.opinion_metadata") == 2


def test_snapshotting_provider_survives_store_failure(tmp_path, caplog):
    class BrokenStore:
        def record_response(self, *args, **kwargs):
            raise OSError("disk full")

    wrapper = SnapshottingCourtListenerProvider(FakeProvider(), BrokenStore())

    assert wrapper.lookup_citations("text") == {"text": "text"}
    assert "Could not persist courtlistener.lookup snapshot" in caplog.text


def test_record_analysis_persists_lossless_d1_and_provision_payload(tmp_path):
    analysis = LegalAnalysis(
        run_id="run-1",
        issue="PTSD rating",
        summary="Smith v. Wilkie",
        how_it_affects_va_claims="The Board must obtain an adequate examination.",
        top_cases=["Smith v. Wilkie (Court of Appeals for Veterans Claims)"],
        surfaced_cases=[
            SurfacedCaseRecord(
                case_title="Smith v. Wilkie",
                court="Court of Appeals for Veterans Claims",
                docket_number="21-1234",
                reporter_citation="33 Vet.App. 10",
                decision_date="2024-01-02",
                official_transcript_url="https://uscourts.cavc.gov/opinions/smith",
                transcript_source_type="official_court_opinion",
                provenance_verified=True,
                retrieved_url="https://uscourts.cavc.gov/opinions/smith",
            )
        ],
        case_abstracts=[
            CaseAbstract(
                case_title="Smith v. Wilkie",
                court="Court of Appeals for Veterans Claims",
                claimant_and_conditions="Veteran claimed PTSD.",
                reasoning_summary="Full reasoning " + ("z" * 400),
                abstract_complete=True,
                abstract_source="full_text",
            )
        ],
        provision_impacts=[
            ProvisionImpact(
                citation="38 C.F.R. § 4.130",
                stage="rating_dc_evaluation",
                legal_effect="clarifying",
                provenance_anchor="The Board must apply 38 C.F.R. § 4.130.",
                m21_1_citations=["M21-1, Part V, Subpart ii"],
            )
        ],
    )
    store = SnapshotStore(tmp_path / "snapshots")

    record = store.record_analysis(analysis)

    assert record.kind == "analysis.report"
    payload = json.loads((store.root / record.path).read_text(encoding="utf-8"))
    assert payload["payload"]["surfaced_cases"][0]["docket_number"] == "21-1234"
    assert payload["payload"]["case_abstracts"][0]["abstract_complete"] is True
    assert payload["payload"]["provision_impacts"][0]["m21_1_citations"]
    assert payload["metadata"]["surfaced_case_count"] == 1


def test_normalize_source_url_malformed_and_userinfo_edge_cases():
    # urlsplit ValueError -> returned unchanged (snapshots.py lines 58-59).
    assert _normalize_source_url("http://[::1") == "http://[::1"
    # No scheme and no netloc -> returned unchanged (line 61).
    assert _normalize_source_url("relative/path") == "relative/path"
    # Scheme but no host -> returned unchanged (line 64).
    assert _normalize_source_url("file:///x") == "file:///x"
    # Invalid port -> port dropped (lines 67-68).
    assert _normalize_source_url("http://host:99999/x") == "http://host/x"
    # Username and password both present (lines 78-81).
    assert _normalize_source_url("http://user:pass@host/x") == (
        "http://user:pass@host/x"
    )
    # Username without a password (line 79->81).
    assert _normalize_source_url("http://user@host/x") == "http://user@host/x"
    # An empty username with a password still takes the username branch.
    assert _normalize_source_url("http://:pass@host/x") == "http://:pass@host/x"
    assert _normalize_source_url("") == ""
    assert _normalize_source_url("   ") == ""


def test_manifest_has_id_tolerates_blank_invalid_and_non_dict_rows(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    # Missing manifest -> False.
    assert store._manifest_has_id("target") is False
    # Blank line (157), invalid JSON (160-161), non-matching dict row (162->154),
    # then a matching row.
    store.manifest.write_text(
        "\nnot json\n"
        + json.dumps({"id": "other"})
        + "\n"
        + json.dumps({"id": "target"})
        + "\n",
        encoding="utf-8",
    )
    assert store._manifest_has_id("target") is True
    assert store._manifest_has_id("absent") is False
    # A non-dict row is skipped and the loop falls through to False (166).
    store.manifest.write_text(json.dumps(["not", "dict"]) + "\n", encoding="utf-8")
    assert store._manifest_has_id("target") is False


def test_manifest_has_id_returns_false_on_oserror(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    # A directory where the manifest file should be makes .open() raise an
    # OSError, which is swallowed into False (snapshots.py lines 164-165).
    store.manifest.mkdir()
    assert store._manifest_has_id("target") is False


def test_snapshot_id_is_the_first_32_hex_chars_of_the_digest(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")

    record = store.record("courtlistener.lookup", {"a": 1})

    assert len(record.id) == 32
    assert record.id == record.sha256[:32]
    assert len(record.sha256) == 64


def test_snapshot_store_creates_nested_root_directories(tmp_path):
    # parents=True: a caller may pass a not-yet-existing nested state dir.
    nested = tmp_path / "deep" / "nested" / "snapshots"
    store = SnapshotStore(nested)

    assert store.root.is_dir()
    assert store.record("k", {"a": 1}).id


def test_snapshot_timestamp_is_whole_second_utc(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")

    record = store.record("k", {"a": 1})

    # Microseconds are stripped so the timestamp is a stable whole second.
    assert "." not in record.captured_at
    assert record.captured_at.endswith("+00:00")

