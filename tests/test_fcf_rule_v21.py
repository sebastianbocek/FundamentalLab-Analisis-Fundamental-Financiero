import unittest

from fundamentallab.criteria import criterion_fcf_growth
from fundamentallab.models import CompanyContext


def make_ctx(fcf):
    return CompanyContext(
        market="USA",
        symbol="TEST",
        name="Test Co",
        quarters=["Q1", "Q2", "Q3", "Q4"],
        revenue=[100, 100, 100, 100],
        net_income=[10, 10, 10, 10],
        cfo=[0, 0, 0, 0],
        capex=[0, 0, 0, 0],
        fcf=fcf,
        buybacks=[1, 1, 1, 1],
        diluted_shares=[10, 10, 10, 10],
        price=10,
    )


class FCFCriterionV21Tests(unittest.TestCase):
    def test_intermediate_quarters_may_fall_and_still_pass(self):
        result = criterion_fcf_growth(make_ctx([100, 70, 90, 110]))
        self.assertTrue(result.passed)

    def test_latest_must_be_greater_than_three_quarters_ago(self):
        result = criterion_fcf_growth(make_ctx([100, 130, 120, 99]))
        self.assertFalse(result.passed)

    def test_equal_does_not_pass(self):
        result = criterion_fcf_growth(make_ctx([100, 80, 120, 100]))
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
