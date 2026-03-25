import uuid
from django.db import connection
from django.test import TestCase
from core.test_helpers import LogInHelper
from payroll.models import BenefitConsumption, BenefitConsumptionStatus
from payroll.services import BenefitConsumptionService
from invoice.trigger_sync import (
    parse_pattern,
    pattern_to_pg_expr,
    pattern_to_mssql_expr,
    sync_trigger,
    DEFAULT_BENEFIT_CODE_PATTERN,
    _get_model_columns,
)
from individual.models import Individual


class BenefitCodePatternTests(TestCase):
    """Tests for benefit code pattern SQL generation."""

    def test_pg_expr_default_benefit_pattern(self):
        expr = pattern_to_pg_expr("BEN-[YY]-[SEQ:10]", "benefit_code_seq")
        self.assertIn("to_char(now(), 'YY')", expr)
        self.assertIn("nextval('benefit_code_seq')", expr)
        self.assertIn("lpad(", expr)

    def test_mssql_expr_default_benefit_pattern(self):
        expr = pattern_to_mssql_expr("BEN-[YY]-[SEQ:10]", "benefit_code_seq")
        self.assertIn("NEXT VALUE FOR benefit_code_seq", expr)
        self.assertIn("'BEN-'", expr)


class BenefitColumnIntrospectionTests(TestCase):
    """Tests for BenefitConsumption column extraction."""

    def test_benefit_columns_include_expected(self):
        columns = _get_model_columns(BenefitConsumption)
        for expected in ('UUID', 'code', 'Amount', 'Type'):
            self.assertIn(expected, columns, f"Expected column {expected}")

    def test_benefit_columns_no_duplicates(self):
        columns = list(f.column for f in BenefitConsumption._meta.concrete_fields)
        self.assertEqual(len(columns), len(set(columns)))


class BenefitTriggerSyncTests(TestCase):
    """Tests for benefit trigger sync detection and application."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username='benefit_trigger_test')
        cls.individual = Individual(first_name="Trigger", last_name="Test", dob="1990-01-01")
        cls.individual.save(user=cls.user)

    def test_sync_detects_in_sync(self):
        updated = sync_trigger(
            model=BenefitConsumption,
            sequence_name='benefit_code_seq',
            trigger_name='benefit_code_trigger',
            code_column='code',
            pattern=DEFAULT_BENEFIT_CODE_PATTERN,
            pg_function_name='set_benefit_code',
            dry_run=True,
        )
        if connection.vendor in ('postgresql', 'microsoft'):
            self.assertFalse(updated)
        else:
            self.assertIsNone(updated)

    def test_sync_detects_pattern_change(self):
        updated = sync_trigger(
            model=BenefitConsumption,
            sequence_name='benefit_code_seq',
            trigger_name='benefit_code_trigger',
            code_column='code',
            pattern='CHANGED-[YYYY]-[SEQ:6]',
            pg_function_name='set_benefit_code',
            dry_run=True,
        )
        if connection.vendor in ('postgresql', 'microsoft'):
            self.assertTrue(updated)

    def test_sync_applies_custom_pattern(self):
        if connection.vendor not in ('postgresql', 'microsoft'):
            self.skipTest("Trigger sync only works on PostgreSQL and MSSQL")

        custom_pattern = 'PAY-[YYYY][MM]-[SEQ:6]'

        bc = None
        try:
            sync_trigger(
                model=BenefitConsumption,
                sequence_name='benefit_code_seq',
                trigger_name='benefit_code_trigger',
                code_column='code',
                pattern=custom_pattern,
                pg_function_name='set_benefit_code',
            )
            bc = BenefitConsumption(
                id=uuid.uuid4(),
                individual=self.individual,
                code='', amount=100,
                status=BenefitConsumptionStatus.ACCEPTED,
                user_created=self.user, user_updated=self.user, version=1,
            )
            bc.save(user=self.user)
            bc.refresh_from_db()
            self.assertTrue(
                bc.code.startswith('PAY-'),
                f"Expected PAY- prefix, got: {bc.code!r}"
            )
        finally:
            sync_trigger(
                model=BenefitConsumption,
                sequence_name='benefit_code_seq',
                trigger_name='benefit_code_trigger',
                code_column='code',
                pattern=DEFAULT_BENEFIT_CODE_PATTERN,
                pg_function_name='set_benefit_code',
            )
            if bc and bc.id:
                BenefitConsumption.objects.filter(id=bc.id).delete()

    def test_sync_preserves_explicit_codes(self):
        if connection.vendor not in ('postgresql', 'microsoft'):
            self.skipTest("Trigger sync only works on PostgreSQL and MSSQL")

        bc = BenefitConsumption(
            id=uuid.uuid4(),
            individual=self.individual,
            code='EXPLICIT-BC', amount=50,
            status=BenefitConsumptionStatus.ACCEPTED,
            user_created=self.user, user_updated=self.user, version=1,
        )
        bc.save(user=self.user)
        bc.refresh_from_db()
        self.assertEqual(bc.code, 'EXPLICIT-BC')
        BenefitConsumption.objects.filter(id=bc.id).delete()

    def test_bulk_create_history_has_db_assigned_codes(self):
        """Verify history records are patched with DB-assigned codes after bulk create."""
        benefits = [
            BenefitConsumption(
                id=uuid.uuid4(),
                individual=self.individual,
                code='', amount=100,
                status=BenefitConsumptionStatus.ACCEPTED,
                user_created=self.user, user_updated=self.user, version=1,
            )
            for _ in range(3)
        ]
        service = BenefitConsumptionService(self.user)
        created = service.bulk_create(benefits)

        for bc in created:
            self.assertTrue(bc.code, "Code should be populated after bulk create")
            history = BenefitConsumption.history.filter(id=bc.id, history_type='+').first()
            self.assertIsNotNone(history, f"Expected history record for BenefitConsumption {bc.id}")
            self.assertEqual(
                history.code, bc.code,
                f"History code {history.code!r} should match {bc.code!r}"
            )
