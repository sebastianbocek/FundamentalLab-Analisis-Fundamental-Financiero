import unittest

from fundamentallab.criteria import intrinsic_value_per_share
from fundamentallab.models import CompanyContext
from fundamentallab.sec_companyfacts import standalone_quarters, extract_companyfacts_context


def rows(vals):
    return vals


def fact_payload():
    # Fiscal year Jan-Dec. CFO/CapEx/buybacks reported YTD; revenue and NI direct.
    def usd(rows):
        return {"units": {"USD": rows}}
    def shares(rows):
        return {"units": {"shares": rows}}

    rev = [
        {"start":"2025-01-01","end":"2025-03-31","val":100,"form":"10-Q","filed":"2025-05-01"},
        {"start":"2025-04-01","end":"2025-06-30","val":110,"form":"10-Q","filed":"2025-08-01"},
        {"start":"2025-07-01","end":"2025-09-30","val":120,"form":"10-Q","filed":"2025-11-01"},
        {"start":"2025-10-01","end":"2025-12-31","val":130,"form":"10-K","filed":"2026-02-01"},
    ]
    ni = [
        {"start":"2025-01-01","end":"2025-03-31","val":10,"form":"10-Q","filed":"2025-05-01"},
        {"start":"2025-04-01","end":"2025-06-30","val":13,"form":"10-Q","filed":"2025-08-01"},
        {"start":"2025-07-01","end":"2025-09-30","val":16,"form":"10-Q","filed":"2025-11-01"},
        {"start":"2025-10-01","end":"2025-12-31","val":20,"form":"10-K","filed":"2026-02-01"},
    ]
    cfo = [
        {"start":"2025-01-01","end":"2025-03-31","val":100,"form":"10-Q","filed":"2025-05-01"},
        {"start":"2025-01-01","end":"2025-06-30","val":230,"form":"10-Q","filed":"2025-08-01"},
        {"start":"2025-01-01","end":"2025-09-30","val":390,"form":"10-Q","filed":"2025-11-01"},
        {"start":"2025-01-01","end":"2025-12-31","val":560,"form":"10-K","filed":"2026-02-01"},
    ]
    cap = [
        {"start":"2025-01-01","end":"2025-03-31","val":20,"form":"10-Q","filed":"2025-05-01"},
        {"start":"2025-01-01","end":"2025-06-30","val":45,"form":"10-Q","filed":"2025-08-01"},
        {"start":"2025-01-01","end":"2025-09-30","val":75,"form":"10-Q","filed":"2025-11-01"},
        {"start":"2025-01-01","end":"2025-12-31","val":110,"form":"10-K","filed":"2026-02-01"},
    ]
    buy = [
        {"start":"2025-01-01","end":"2025-03-31","val":1,"form":"10-Q","filed":"2025-05-01"},
        {"start":"2025-01-01","end":"2025-06-30","val":3,"form":"10-Q","filed":"2025-08-01"},
        {"start":"2025-01-01","end":"2025-09-30","val":6,"form":"10-Q","filed":"2025-11-01"},
        {"start":"2025-01-01","end":"2025-12-31","val":10,"form":"10-K","filed":"2026-02-01"},
    ]
    diluted = [
        {"start":"2025-01-01","end":"2025-03-31","val":10.0,"form":"10-Q","filed":"2025-05-01"},
        {"start":"2025-04-01","end":"2025-06-30","val":9.9,"form":"10-Q","filed":"2025-08-01"},
        {"start":"2025-07-01","end":"2025-09-30","val":9.8,"form":"10-Q","filed":"2025-11-01"},
    ]
    outstanding = [
        {"end":"2025-04-20","val":10.0,"form":"10-Q","filed":"2025-05-01"},
        {"end":"2025-07-20","val":9.9,"form":"10-Q","filed":"2025-08-01"},
        {"end":"2025-10-20","val":9.8,"form":"10-Q","filed":"2025-11-01"},
        {"end":"2026-01-20","val":9.7,"form":"10-K","filed":"2026-02-01"},
    ]
    return {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": usd(rev),
                "NetIncomeLoss": usd(ni),
                "NetCashProvidedByUsedInOperatingActivities": usd(cfo),
                "PaymentsToAcquirePropertyPlantAndEquipment": usd(cap),
                "PaymentsForRepurchaseOfCommonStock": usd(buy),
                "WeightedAverageNumberOfDilutedSharesOutstanding": shares(diluted),
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": shares(outstanding),
            }
        }
    }


class CompanyFactsCoverageTests(unittest.TestCase):
    def test_reconstructs_ytd_cashflow_into_quarters(self):
        payload = fact_payload()
        rows = payload["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"]
        q = list(standalone_quarters([
            {
                "start": __import__('datetime').datetime.strptime(r['start'],'%Y-%m-%d').date(),
                "end": __import__('datetime').datetime.strptime(r['end'],'%Y-%m-%d').date(),
                "filed": __import__('datetime').datetime.strptime(r['filed'],'%Y-%m-%d').date(),
                "form": r['form'], "val": float(r['val'])
            } for r in rows
        ]).values())
        self.assertEqual(q[-4:], [100.0, 130.0, 160.0, 170.0])

    def test_enrichment_builds_four_fcf_quarters(self):
        base = CompanyContext(
            market="USA", symbol="TEST", name="Test", quarters=["A","B","C","D"],
            revenue=[100,110,120,130], net_income=[10,13,16,20],
            cfo=[None,None,None,None], capex=[None,None,None,None],
            fcf=[None,None,None,None], buybacks=[1,2,3,4],
            diluted_shares=[10,9.9,9.8,None], metadata={"cik":1}
        )
        e = extract_companyfacts_context(base, fact_payload())
        self.assertEqual(e.fcf, [80.0, 105.0, 130.0, 135.0])
        self.assertEqual(len(e.quarters), 4)
        self.assertIsNotNone(e.metadata.get("latest_fy_fcf"))

    def test_dcf_uses_annual_fallback_when_quarters_missing(self):
        ctx = CompanyContext(
            market="USA", symbol="X", name="X", quarters=["Q1","Q2","Q3","Q4"],
            revenue=[100,105,110,115], net_income=[10,11,12,13],
            cfo=[None,None,None,None], capex=[None,None,None,None],
            fcf=[None,None,None,None], buybacks=[None,None,None,None],
            diluted_shares=[None,None,None,10], price=5,
            metadata={"latest_fy_fcf":100, "latest_fy_fcf_source":"test annual FY"},
        )
        value, reason = intrinsic_value_per_share(ctx)
        self.assertIsNotNone(value)
        self.assertIn("test annual FY", reason)


if __name__ == "__main__":
    unittest.main()
