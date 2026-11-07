import tempfile
import unittest
from pathlib import Path

from fundamentallab.models import CompanyContext
from fundamentallab.criteria import criterion_below_intrinsic, criterion_fcf_growth, criterion_net_margin_growth, intrinsic_value_per_share
from fundamentallab.database import connect, store_result, create_run
from fundamentallab.engine import evaluate_company


def ctx(**kw):
    d=dict(
        market='USA',symbol='T',name='Test',quarters=['Q1','Q2','Q3','Q4'],
        revenue=[100,110,120,130],net_income=[10,12,14,17],
        cfo=[20,22,25,28],capex=[5,5,5,5],fcf=[15,17,20,23],
        buybacks=[1,2,3,4],diluted_shares=[10,None,None,9.8],
        price=10,currency='USD',metadata={}
    );d.update(kw);return CompanyContext(**d)


class V23Tests(unittest.TestCase):
    def test_dcf_works_with_partial_share_history(self):
        value, reason=intrinsic_value_per_share(ctx())
        self.assertIsNotNone(value)
        self.assertGreater(value,0)

    def test_intrinsic_missing_shares_is_nd(self):
        r=criterion_below_intrinsic(ctx(diluted_shares=[None,None,None,None], metadata={}))
        self.assertFalse(r.passed)
        self.assertFalse(r.evaluable)

    def test_missing_margin_is_nd(self):
        r=criterion_net_margin_growth(ctx(revenue=[100,None,120,130]))
        self.assertFalse(r.evaluable)

    def test_missing_fcf_is_nd(self):
        r=criterion_fcf_growth(ctx(fcf=[15,None,20,23]))
        self.assertFalse(r.evaluable)

    def test_price_missing_is_nd_but_intrinsic_survives(self):
        r=criterion_below_intrinsic(ctx(price=None))
        self.assertFalse(r.evaluable)
        self.assertIsInstance(r.value, dict)
        self.assertIsNotNone(r.value.get('intrinsic'))

    def test_database_serializes_evaluable(self):
        with tempfile.TemporaryDirectory() as d:
            conn=connect(Path(d)/'x.sqlite')
            run=create_run(conn,['USA'])
            result=evaluate_company(ctx(price=None))
            store_result(conn,run,result);conn.commit()
            raw=conn.execute('SELECT criteria_json FROM screen_results LIMIT 1').fetchone()[0]
            self.assertIn('"evaluable"', raw)

if __name__=='__main__': unittest.main()
