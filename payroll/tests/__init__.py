# flake8: noqa

from payroll.tests.payment_point_gql_tests import PaymentPointGQLTestCase
from payroll.tests.payroll_gql_tests import PayrollGQLTestCase
from payroll.tests.payroll_async_tests import PayrollAsyncTests
try:
    from payroll.tests.test_trigger_sync import (
        BenefitCodePatternTests, BenefitColumnIntrospectionTests, BenefitTriggerSyncTests
    )
except ImportError:
    pass
