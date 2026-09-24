import copy
import unittest

from us_equity_research.core.contracts import ContractError
from us_equity_research.ingest.filing_body import analyze_bodies, extract_body, numeric, sec_url

URL = "https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm"
NOW = "2026-09-13T10:00:00+00:00"
EVIDENCE = {"source_url": URL, "published_at": "2026-07-29T20:08:01+00:00"}


def document(short=""):
    return (
        """<xbrli:context id="C"><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
    <xbrli:context id="D"><xbrli:entity><xbrli:segment><xbrldi:explicitMember dimension="x">y</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
    <xbrli:unit id="U"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
    <ix:nonFraction name="us-gaap:LongTermDebtCurrent" contextRef="C" unitRef="U" scale="6" format="ixt:num-dot-decimal">9,227</ix:nonFraction>
    <ix:nonFraction name="us-gaap:LongTermDebtNoncurrent" contextRef="C" unitRef="U" scale="6">31,067</ix:nonFraction>
    <ix:nonFraction name="us-gaap:LongTermDebtCurrent" contextRef="D" unitRef="U" scale="6">888</ix:nonFraction>"""
        + short
    )


class FilingBodyTests(unittest.TestCase):
    def test_units_dimensions_and_debt_are_conservative(self):
        body = extract_body(document(), EVIDENCE, "MSFT", NOW)
        self.assertEqual(len(body["facts"]), 2)
        bundle = {"schema_version": "us-filing-body-1", "documents": [body]}
        result = analyze_bodies(bundle, None, NOW)[0]
        self.assertEqual(result["long_term_debt_including_current"], 40294000000)
        self.assertIsNone(result["total_debt"])
        self.assertEqual(result["debt_reconciliation"], "UNKNOWN")
        self.assertEqual(body["facts"][0]["available_at"], NOW)

    def test_nil_missing_and_zero_differ(self):
        self.assertIsNone(numeric({"text": "0", "xsi:nil": "true"}))
        self.assertIsNone(numeric({"text": "—"}))
        self.assertEqual(numeric({"text": "—", "format": "ixt:zero-dash"}), 0)
        self.assertEqual(numeric({"text": "2.5", "scale": "6", "sign": "-"}), -2500000)
        self.assertIsNone(numeric({"text": "2", "format": "unsupported"}))

    def test_conflict_rejected(self):
        extra = '<ix:nonFraction name="us-gaap:LongTermDebtCurrent" contextRef="C" unitRef="U" scale="6">1</ix:nonFraction>'
        with self.assertRaises(ContractError):
            extract_body(document(extra), EVIDENCE, "MSFT", NOW)

    def test_cross_check_and_late_availability(self):
        body = extract_body(document(), EVIDENCE, "MSFT", NOW)
        bundle = {"schema_version": "us-filing-body-1", "documents": [body]}
        facts = copy.deepcopy(body["facts"])
        facts[1]["value"] += 1
        result = analyze_bodies(bundle, {"companies": [{"symbol": "MSFT", "facts": facts}]}, NOW)[0]
        self.assertEqual([x["status"] for x in result["checks"]], ["MATCH", "CONFLICT"])
        with self.assertRaises(ContractError):
            analyze_bodies(bundle, None, "2026-09-12T10:00:00+00:00")

    def test_source_restriction(self):
        for url in [
            "https://evil.test/a.htm",
            URL + "?token=x",
            URL.replace("www.sec.gov", "www.sec.gov.evil.test"),
        ]:
            with self.assertRaises(ContractError):
                sec_url(url)

    def test_period_mismatch_prevents_sum(self):
        html = document().replace(
            'name="us-gaap:LongTermDebtNoncurrent" contextRef="C"',
            'name="us-gaap:LongTermDebtNoncurrent" contextRef="OLD"',
        )
        html += '<xbrli:context id="OLD"><xbrli:period><xbrli:instant>2025-06-30</xbrli:instant></xbrli:period></xbrli:context>'
        body = extract_body(html, EVIDENCE, "MSFT", NOW)
        result = analyze_bodies(
            {"schema_version": "us-filing-body-1", "documents": [body]}, None, NOW
        )[0]
        self.assertIsNone(result["long_term_debt_including_current"])
