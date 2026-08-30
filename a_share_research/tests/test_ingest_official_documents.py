from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from a_share_research.core.contracts import ContractError
from a_share_research.core.pipeline import run_research
from a_share_research.core.snapshot import validate_snapshot
from a_share_research.ingest import (
    OfficialDocumentError,
    OfficialFetchRequest,
    OfficialFetchResponse,
    collect_official_evidence,
    normalize_official_discovery_output,
    validate_official_discovery,
    validate_official_url,
)


class FakeOfficialTransport:
    def __init__(self, responses: dict[str, OfficialFetchResponse]) -> None:
        self.responses = responses
        self.requests: list[OfficialFetchRequest] = []

    def fetch(self, request: OfficialFetchRequest) -> OfficialFetchResponse:
        self.requests.append(request)
        return self.responses[request.url]


class OfficialDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.raw_store = self.root / "official-raw"
        self.workspace.mkdir()
        self.window_start = "2026-08-27T00:00:00+08:00"
        self.window_end = "2026-08-29T23:59:59+08:00"
        self.url = "https://static.cninfo.com.cn/finalpage/2026-08-28/test.html"
        self.retrieved_at = datetime(2026, 8, 30, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_normalizes_one_wrapped_contract_and_rejects_two(self) -> None:
        payload = self._discovery()
        wrapped = "先核验来源。\n" + json.dumps(payload, ensure_ascii=False) + "\n完成。"

        normalized = normalize_official_discovery_output(
            wrapped,
            window_start=self.window_start,
            window_end=self.window_end,
        )

        self.assertEqual(normalized["sources"][0]["issuer"]["security_id"], "CN.SH.600000")
        with self.assertRaisesRegex(ContractError, "exactly one"):
            normalize_official_discovery_output(
                json.dumps(payload, ensure_ascii=False) + json.dumps(payload, ensure_ascii=False),
                window_start=self.window_start,
                window_end=self.window_end,
            )

    def test_rejects_nonofficial_domains_sensitive_urls_and_wrong_issuer(self) -> None:
        with self.assertRaisesRegex(ContractError, "allowlist"):
            validate_official_url("https://example.com/report.pdf", "company_disclosure")
        with self.assertRaisesRegex(ContractError, "sensitive"):
            validate_official_url(
                "https://static.cninfo.com.cn/report.pdf?token=secret",
                "company_disclosure",
            )

        payload = self._discovery()
        payload["sources"][0]["issuer"]["security_id"] = "CN.SZ.600000"
        with self.assertRaisesRegex(ContractError, "must be CN.SH.600000"):
            validate_official_discovery(payload)

        policy_with_issuer = self._discovery()
        policy_with_issuer["sources"][0]["category"] = "policy"
        policy_with_issuer["sources"][0]["source_url"] = (
            "https://www.miit.gov.cn/zwgk/zcwj/test.html"
        )
        with self.assertRaisesRegex(ContractError, "issuer must be null"):
            validate_official_discovery(policy_with_issuer)

        company_without_issuer = self._discovery()
        company_without_issuer["sources"][0]["issuer"] = None
        with self.assertRaisesRegex(ContractError, "must be an object"):
            validate_official_discovery(company_without_issuer)

    def test_collects_verified_html_extracts_labeled_amount_and_generates_direct_candidate(
        self,
    ) -> None:
        body = _company_html()
        transport = FakeOfficialTransport({self.url: self._response(body)})

        result = collect_official_evidence(
            _empty_seed(),
            self._discovery(),
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=transport,
        )

        self.assertEqual(result.receipt["accepted_source_count"], 1)
        self.assertEqual(result.receipt["generated_candidate_count"], 1)
        self.assertEqual(result.receipt["rejected_source_count"], 0)
        evidence = result.seed["evidence"][0]
        self.assertEqual(evidence["source_level"], "official")
        self.assertEqual(evidence["document"]["extraction_method"], "html_text")
        self.assertEqual(evidence["facts"][0]["metric"], "disclosed_contract_amount_cny")
        self.assertEqual(evidence["facts"][0]["value"], "1250000000.0")
        self.assertEqual(
            evidence["official_provenance"]["time_semantics"], "official_datetime_verified"
        )
        candidate = result.seed["themes"][0]["candidates"][0]
        self.assertEqual(candidate["security_id"], "CN.SH.600000")
        self.assertEqual(candidate["impact_chain"][0]["status"], "supported")
        self.assertEqual(candidate["impact_chain"][2]["status"], "unknown")
        self.assertEqual(
            candidate["opportunity_profile"]["economic_impact"]["magnitude"]["status"], "partial"
        )
        self.assertEqual(result.seed["themes"][0]["next_catalyst_at"], None)
        self.assertFalse(result.receipt["raw_documents_embedded_in_receipt"])
        self.assertNotIn(str(self.raw_store), json.dumps(result.receipt, ensure_ascii=False))

        artifact_id = result.receipt["accepted"][0]["raw_artifact_id"]
        artifact = self.raw_store / artifact_id
        self.assertEqual((artifact / "source.bin").read_bytes(), body)
        self.assertEqual((artifact / "source.bin").stat().st_mode & 0o777, 0o600)

    def test_repeated_bytes_reuse_first_seen_but_revision_gets_new_artifact(self) -> None:
        first = collect_official_evidence(
            _empty_seed(),
            self._discovery(),
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({self.url: self._response(_company_html())}),
        )
        later_response = self._response(
            _company_html(),
            retrieved_at=datetime(2026, 8, 30, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        repeated = collect_official_evidence(
            _empty_seed(),
            self._discovery(),
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({self.url: later_response}),
        )
        self.assertFalse(repeated.receipt["accepted"][0]["raw_artifact_created"])
        self.assertEqual(
            first.receipt["accepted"][0]["first_seen_at"],
            repeated.receipt["accepted"][0]["first_seen_at"],
        )
        self.assertEqual(
            repeated.receipt["accepted"][0]["retrieved_at"], later_response.retrieved_at.isoformat()
        )

        repeated_on_compiled_seed = collect_official_evidence(
            first.seed,
            self._discovery(),
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({self.url: later_response}),
        )
        self.assertEqual(repeated_on_compiled_seed.seed, first.seed)
        self.assertEqual(repeated_on_compiled_seed.receipt["accepted_source_count"], 1)

        revised_body = _company_html().replace(b"12.5", b"13.5")
        revised = collect_official_evidence(
            first.seed,
            self._discovery(),
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({self.url: self._response(revised_body)}),
        )
        self.assertEqual(revised.seed, first.seed)
        self.assertEqual(revised.receipt["accepted_source_count"], 0)
        self.assertEqual(revised.receipt["rejected_source_count"], 1)
        self.assertIn("different frozen document", revised.receipt["rejected"][0]["reason"])
        raw_artifact_ids = {path.name for path in self.raw_store.iterdir() if path.is_dir()}
        self.assertEqual(len(raw_artifact_ids), 2)
        self.assertIn(first.receipt["accepted"][0]["raw_artifact_id"], raw_artifact_ids)

    def test_generated_candidate_runs_through_the_canonical_engine_with_unknowns_intact(
        self,
    ) -> None:
        collected = collect_official_evidence(
            _empty_seed(),
            self._discovery(),
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({self.url: self._response(_company_html())}),
        )
        market_id = "MKT-TEST-SH-600000-20260828"
        collected.seed["themes"][0]["candidates"][0]["market_evidence_refs"] = [market_id]
        snapshot = {
            **collected.seed,
            "snapshot_id": "official-candidate-integration",
            "data_mode": "snapshot",
            "pit_quality": "RECONSTRUCTED_NON_PIT",
            "as_of": "2026-08-28T15:00:00+08:00",
            "retrieved_at": "2026-08-30T01:05:00+08:00",
            "market_context": {
                "regime": "UNKNOWN",
                "breadth": "UNKNOWN",
                "liquidity": "UNKNOWN",
                "calculation_note": "Deterministic integration-test observation.",
                "evidence_refs": [market_id],
            },
            "evidence": [
                *collected.seed["evidence"],
                {
                    "evidence_id": market_id,
                    "category": "market_data",
                    "source_level": "structured_market",
                    "title": "Candidate market observation",
                    "source_url": "https://www.baostock.com/",
                    "published_at": "2026-08-28T15:05:00+08:00",
                    "effective_at": "2026-08-28T15:00:00+08:00",
                    "available_at": "2026-08-28T15:05:00+08:00",
                    "retrieved_at": "2026-08-30T01:05:00+08:00",
                    "as_of": "2026-08-28T15:00:00+08:00",
                    "summary": "Market observation exists; pricing details remain UNKNOWN.",
                },
            ],
        }
        validate_snapshot(snapshot)
        snapshot_path = (
            self.workspace / "data" / "normalized" / snapshot["snapshot_id"] / "snapshot.json"
        )
        snapshot_path.parent.mkdir(parents=True)
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

        summary = run_research(
            {
                "schema_version": "0.1",
                "market": "CN",
                "workflow": "daily_report",
                "decision_at": "2026-08-30T02:00:00+08:00",
                "snapshot": {"selector": "id", "snapshot_id": snapshot["snapshot_id"]},
                "top_n": 5,
            },
            self.workspace,
        )
        packet = json.loads(
            (
                self.workspace / "artifacts" / "runs" / summary["run_id"] / "research_packet.json"
            ).read_text(encoding="utf-8")
        )
        decision = packet["all_decisions"][0]
        self.assertEqual(decision["decision"], "continue_research")
        self.assertEqual(decision["next_catalyst_at"], None)
        self.assertIn(
            "expectation_gap", decision["opportunity_profile"]["missing_opportunity_factors"]
        )

    def test_quote_mismatch_is_rejected_without_poisoning_the_base_seed(self) -> None:
        body = _company_html().replace("浦发银行".encode(), "另一家公司".encode())
        result = collect_official_evidence(
            _empty_seed(),
            self._discovery(),
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({self.url: self._response(body)}),
        )

        self.assertEqual(result.receipt["accepted_source_count"], 0)
        self.assertEqual(result.receipt["rejected_source_count"], 1)
        self.assertEqual(result.seed, _empty_seed())
        self.assertIn("issuer quotes", result.receipt["rejected"][0]["reason"])

    def test_policy_is_frozen_as_a_lead_without_inventing_a_candidate(self) -> None:
        url = "https://www.miit.gov.cn/zwgk/zcwj/wjfb/test.html"
        payload = self._discovery()
        payload["sources"] = [
            {
                "source_url": url,
                "category": "policy",
                "title": "关于公开征求智能制造指导意见的通知",
                "title_quote": "关于公开征求智能制造指导意见的通知",
                "published_at": "2026-08-28T00:00:00+08:00",
                "published_at_precision": "date",
                "published_at_quote": "发布日期：2026-08-28",
                "effective_at": None,
                "effective_at_quote": None,
                "issuer": None,
            }
        ]
        body = (
            "<html><body><h1>关于公开征求智能制造指导意见的通知</h1>"
            "<p>发布日期：2026-08-28</p><p>有关单位可反馈意见。</p></body></html>"
        ).encode()
        response = OfficialFetchResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=body,
            retrieved_at=self.retrieved_at,
        )

        result = collect_official_evidence(
            _empty_seed(),
            payload,
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({url: response}),
        )

        self.assertEqual(result.receipt["policy_lead_count"], 1)
        self.assertEqual(result.receipt["generated_candidate_count"], 0)
        self.assertEqual(result.seed["themes"][0]["candidates"], [])
        self.assertIn(
            "未从政策或一般公告自动推断受益公司",
            result.seed["themes"][0]["dimensions"]["many"]["reason"],
        )

    def test_wrong_media_type_and_redirect_are_rejected(self) -> None:
        for label, response in (
            (
                "media",
                OfficialFetchResponse(
                    requested_url=self.url,
                    final_url=self.url,
                    status=200,
                    headers={"Content-Type": "application/octet-stream"},
                    body=_company_html(),
                    retrieved_at=self.retrieved_at,
                ),
            ),
            (
                "redirect",
                OfficialFetchResponse(
                    requested_url=self.url,
                    final_url=self.url + ".redirected",
                    status=200,
                    headers={"Content-Type": "text/html"},
                    body=_company_html(),
                    retrieved_at=self.retrieved_at,
                ),
            ),
        ):
            with self.subTest(label=label):
                result = collect_official_evidence(
                    _empty_seed(),
                    self._discovery(),
                    workspace=self.workspace,
                    raw_store_root=self.raw_store,
                    transport=FakeOfficialTransport({self.url: response}),
                )
                self.assertEqual(result.receipt["accepted_source_count"], 0)
                self.assertEqual(result.receipt["rejected_source_count"], 1)

    def test_raw_store_must_be_private_and_outside_the_workspace(self) -> None:
        transport = FakeOfficialTransport({self.url: self._response(_company_html())})
        with self.assertRaisesRegex(OfficialDocumentError, "outside the runtime workspace"):
            collect_official_evidence(
                _empty_seed(),
                self._discovery(),
                workspace=self.workspace,
                raw_store_root=self.workspace / "raw",
                transport=transport,
            )

        target = self.root / "real-raw"
        target.mkdir()
        symlink = self.root / "raw-link"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(OfficialDocumentError, "symbolic link"):
            collect_official_evidence(
                _empty_seed(),
                self._discovery(),
                workspace=self.workspace,
                raw_store_root=symlink,
                transport=transport,
            )

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "official PDF extra is not installed")
    def test_pdf_text_layer_is_extracted_without_ocr(self) -> None:
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        url = "https://www.miit.gov.cn/zwgk/zcwj/policy.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        content.set_data(
            b"BT /F1 12 Tf 72 720 Td (Official Policy Notice) Tj "
            b"0 -20 Td (Publication: 2026-08-28) Tj ET"
        )
        page[NameObject("/Contents")] = writer._add_object(content)
        output = io.BytesIO()
        writer.write(output)
        discovery = {
            "schema_version": "0.1",
            "market": "CN",
            "discovery_window": {"start": self.window_start, "end": self.window_end},
            "sources": [
                {
                    "source_url": url,
                    "category": "policy",
                    "title": "Official Policy Notice",
                    "title_quote": "Official Policy Notice",
                    "published_at": "2026-08-28T00:00:00+08:00",
                    "published_at_precision": "date",
                    "published_at_quote": "Publication: 2026-08-28",
                    "effective_at": None,
                    "effective_at_quote": None,
                    "issuer": None,
                }
            ],
        }
        response = OfficialFetchResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"Content-Type": "application/pdf"},
            body=output.getvalue(),
            retrieved_at=self.retrieved_at,
        )

        result = collect_official_evidence(
            _empty_seed(),
            discovery,
            workspace=self.workspace,
            raw_store_root=self.raw_store,
            transport=FakeOfficialTransport({url: response}),
        )

        self.assertEqual(result.receipt["accepted_source_count"], 1)
        self.assertEqual(result.seed["evidence"][0]["document"]["media_type"], "application/pdf")
        self.assertEqual(
            result.seed["evidence"][0]["document"]["extraction_method"],
            "pdf_text_layer",
        )

    def _discovery(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "market": "CN",
            "discovery_window": {"start": self.window_start, "end": self.window_end},
            "sources": [
                {
                    "source_url": self.url,
                    "category": "company_disclosure",
                    "title": "关于签订重大合同的公告",
                    "title_quote": "关于签订重大合同的公告",
                    "published_at": "2026-08-28T09:30:00+08:00",
                    "published_at_precision": "datetime",
                    "published_at_quote": "发布时间：2026-08-28 09:30",
                    "effective_at": None,
                    "effective_at_quote": None,
                    "issuer": {
                        "security_id": "CN.SH.600000",
                        "symbol": "600000",
                        "name": "浦发银行",
                        "name_quote": "浦发银行",
                        "symbol_quote": "证券代码：600000",
                    },
                }
            ],
        }

    def _response(
        self,
        body: bytes,
        *,
        retrieved_at: datetime | None = None,
    ) -> OfficialFetchResponse:
        return OfficialFetchResponse(
            requested_url=self.url,
            final_url=self.url,
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=body,
            retrieved_at=retrieved_at or self.retrieved_at,
        )


def _empty_seed() -> dict[str, Any]:
    return {"schema_version": "0.1", "market": "CN", "evidence": [], "themes": []}


def _company_html() -> bytes:
    return (
        "<html><body><h1>关于签订重大合同的公告</h1>"
        "<p>浦发银行</p><p>证券代码：600000</p>"
        "<p>发布时间：2026-08-28 09:30</p>"
        "<p>合同金额为人民币12.5亿元。</p></body></html>"
    ).encode()


if __name__ == "__main__":
    unittest.main()
