import unittest,tempfile
from datetime import date
from pathlib import Path
from fundamentallab.models import CompanyContext
from fundamentallab.criteria import criterion_fcf_growth,criterion_buyback_growth,criterion_net_margin_growth,criterion_below_intrinsic
from fundamentallab.sec_frames import completed_quarters
from fundamentallab.database import connect,upsert_company,company_count

def ctx(**kw):
    d=dict(market='USA',symbol='T',name='Test',quarters=['Q1','Q2','Q3','Q4'],revenue=[100,110,120,130],net_income=[10,13.2,16.8,20.8],cfo=[20,24,28,34],capex=[5,5,5,5],fcf=[15,19,23,29],buybacks=[1,2,3,4],diluted_shares=[10,10,10,10],price=1,currency='USD');d.update(kw);return CompanyContext(**d)
class Tests(unittest.TestCase):
    def test_green(self):
        c=ctx();self.assertTrue(criterion_fcf_growth(c).passed);self.assertTrue(criterion_buyback_growth(c).passed);self.assertTrue(criterion_net_margin_growth(c).passed);self.assertTrue(criterion_below_intrinsic(c).passed)
    def test_break(self):self.assertFalse(criterion_fcf_growth(ctx(fcf=[3,2,1,2])).passed)
    def test_margin_ratio(self):self.assertFalse(criterion_net_margin_growth(ctx(revenue=[100,200,300,400],net_income=[10,19,27,34])).passed)
    def test_periods(self):self.assertEqual(completed_quarters(4,date(2026,8,29)),['CY2025Q3','CY2025Q4','CY2026Q1','CY2026Q2'])
    def test_db(self):
        with tempfile.TemporaryDirectory() as d:
            c=connect(Path(d)/'x.db');upsert_company(c,'USA','ABC','ABC');c.commit();self.assertEqual(int(company_count(c).iloc[0].companies),1)
if __name__=='__main__':unittest.main()
