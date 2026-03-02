# flake8: noqa

try:
    from payroll.tests.payment_point_gql_tests import PaymentPointGQLTestCase
except ImportError:
    pass
try:
    from payroll.tests.payroll_gql_tests import PayrollGQLTestCase
except ImportError:
    pass
