"""The CSV reconciliation reads and writes only an APPROVE_FOR_PAYMENT payroll.

The blank sheet lists a payroll's benefits for the agency to pay, and the
upload records what the agency paid: like every other payment path, both
refuse a payroll that is not approved for payment, and change nothing.
"""
import uuid
from datetime import date
from io import BytesIO

import pandas as pd
from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import Individual
from payroll.apps import PayrollConfig
from payroll.models import (
    BenefitConsumption, BenefitConsumptionStatus, CsvReconciliationUpload, Payroll,
    PayrollBenefitConsumption, PayrollStatus,
)
from payroll.services import CsvReconciliationService

NOT_APPROVED = (PayrollStatus.PENDING_APPROVAL, PayrollStatus.RECONCILED, PayrollStatus.REJECTED,
                PayrollStatus.FAILED, PayrollStatus.GENERATING)


class CsvReconciliationPayrollStatusTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username='csv_status_user')
        cls.individual = Individual(first_name='Csv', last_name='Statut', dob='1990-01-01')
        cls.individual.save(username=cls.user.username)

    def _payroll(self, status):
        payroll = Payroll(name=f'P-{uuid.uuid4().hex[:6]}', status=status, json_ext={})
        payroll.save(username=self.user.username)
        benefit = BenefitConsumption(
            individual=self.individual, code=f'BEN-{uuid.uuid4().hex[:8]}', amount=72000,
            type='Cash Transfer', status=BenefitConsumptionStatus.ACCEPTED,
            date_due=date(2026, 10, 1), json_ext={})
        benefit.save(username=self.user.username)
        PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(username=self.user.username)
        return payroll, benefit

    def _sheet(self, benefit):
        frame = pd.DataFrame([{
            PayrollConfig.csv_reconciliation_field_mapping['code']: benefit.code,
            PayrollConfig.csv_reconciliation_field_mapping['status']: benefit.status,
            PayrollConfig.csv_reconciliation_receipt_column: 'RCPT-1',
            PayrollConfig.csv_reconciliation_paid_extra_field: PayrollConfig.csv_reconciliation_paid_yes,
        }])
        frame.rename(columns={PayrollConfig.csv_reconciliation_receipt_column:
                              PayrollConfig.csv_reconciliation_field_mapping.get(
                                  PayrollConfig.csv_reconciliation_receipt_column,
                                  PayrollConfig.csv_reconciliation_receipt_column)},
                     inplace=True)
        sheet = BytesIO()
        frame.to_csv(sheet, index=False)
        sheet.seek(0)
        return sheet

    def _upload(self, payroll, benefit):
        upload = CsvReconciliationUpload()
        upload.save(username=self.user.username)
        return CsvReconciliationService(self.user).upload_reconciliation(
            payroll.id, self._sheet(benefit), upload)

    def test_an_approved_payroll_is_downloaded_and_reconciled(self):
        payroll, benefit = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        sheet = CsvReconciliationService(self.user).download_reconciliation(payroll.id)
        self.assertIn(benefit.code, sheet.getvalue().decode())

        _, errors, summary = self._upload(payroll, benefit)

        self.assertIsNone(errors)
        self.assertEqual(summary['affected_rows'], 1)
        benefit.refresh_from_db()
        self.assertEqual((benefit.status, benefit.receipt), (BenefitConsumptionStatus.RECONCILED, 'RCPT-1'))

    def test_a_payroll_not_approved_for_payment_is_not_downloaded(self):
        for status in NOT_APPROVED:
            with self.subTest(status=status):
                payroll, _ = self._payroll(status)
                with self.assertRaisesMessage(ValueError, 'payroll_not_approved_for_payment'):
                    CsvReconciliationService(self.user).download_reconciliation(payroll.id)

    def test_a_payroll_not_approved_for_payment_records_nothing(self):
        for status in NOT_APPROVED:
            with self.subTest(status=status):
                payroll, benefit = self._payroll(status)
                with self.assertRaisesMessage(ValueError, 'payroll_not_approved_for_payment'):
                    self._upload(payroll, benefit)
                benefit.refresh_from_db()
                self.assertEqual((benefit.status, benefit.receipt),
                                 (BenefitConsumptionStatus.ACCEPTED, None))
