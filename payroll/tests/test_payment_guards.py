"""Guards on the paths that send, restore or reconcile a benefit.

- The gateway task sends a payroll only while it is APPROVE_FOR_PAYMENT.
- A refused benefit deletion gives the benefit back the status it had, not
  ACCEPTED.
- A CSV reconciliation adds its columns to the benefit's json_ext and keeps
  the keys already there.
"""
import uuid
from datetime import date
from unittest import mock

import pandas as pd
from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import Individual
from payroll.apps import PayrollConfig
from payroll.models import (
    BenefitConsumption, BenefitConsumptionStatus, Payroll, PayrollBenefitConsumption,
    PayrollStatus,
)
from payroll.services import BenefitConsumptionService, CsvReconciliationService
from payroll.tasks import send_requests_to_gateway_payment


class _Fixtures(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username='payroll_guard_user')
        cls.individual = Individual(first_name='Garde', last_name='Paiement', dob='1990-01-01')
        cls.individual.save(username=cls.user.username)

    def _payroll(self, status):
        payroll = Payroll(name=f'P-{uuid.uuid4().hex[:6]}', status=status,
                          payment_method='StrategyGuardTest', json_ext={})
        payroll.save(username=self.user.username)
        return payroll

    def _benefit(self, status, payroll=None, json_ext=None):
        benefit = BenefitConsumption(
            individual=self.individual, code=f'BEN-{uuid.uuid4().hex[:8]}',
            amount=72000, type='Cash Transfer', status=status, date_due=date(2026, 10, 1),
            json_ext=json_ext if json_ext is not None else {})
        benefit.save(username=self.user.username)
        if payroll is not None:
            PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(
                username=self.user.username)
        return benefit


class GatewayTaskStatusGateTest(_Fixtures):

    def _send(self, payroll):
        strategy = mock.MagicMock()
        with mock.patch('payroll.tasks.PaymentMethodStorage.get_chosen_payment_method',
                        return_value=strategy):
            send_requests_to_gateway_payment(str(payroll.id), str(self.user.id))
        return strategy

    def test_an_approved_payroll_is_sent(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        strategy = self._send(payroll)
        strategy.make_payment_for_payroll.assert_called_once()

    def test_a_payroll_outside_approve_for_payment_is_not_sent(self):
        for status in (PayrollStatus.REJECTED, PayrollStatus.RECONCILED,
                       PayrollStatus.PENDING_APPROVAL, PayrollStatus.FAILED,
                       'PENDING_VERIFICATION'):
            with self.subTest(status=status):
                payroll = self._payroll(status)
                with self.assertLogs('payroll.tasks', level='ERROR'):
                    strategy = self._send(payroll)
                strategy.initialize_payment_gateway.assert_not_called()
                strategy.make_payment_for_payroll.assert_not_called()

    def test_a_deleted_payroll_is_not_sent(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        Payroll.objects.filter(id=payroll.id).update(is_deleted=True)
        with self.assertLogs('payroll.tasks', level='ERROR'):
            strategy = self._send(payroll)
        strategy.make_payment_for_payroll.assert_not_called()


def restore_benefit_after_refused_deletion(benefit, user):
    from payroll.services import restore_benefit_after_refused_deletion as restore
    return restore(benefit, user)


class RefusedDeletionTest(_Fixtures):

    def _request_deletion(self, benefit):
        with mock.patch('payroll.services.TaskService'):
            BenefitConsumptionService(self.user).delete({'id': benefit.id})
        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.PENDING_DELETION)
        return benefit

    def test_the_status_before_the_request_comes_back(self):
        cases = (
            (PayrollStatus.APPROVE_FOR_PAYMENT, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT),
            (PayrollStatus.APPROVE_FOR_PAYMENT, BenefitConsumptionStatus.ACCEPTED),
            (PayrollStatus.PENDING_APPROVAL, BenefitConsumptionStatus.ACCEPTED),
            (PayrollStatus.REJECTED, BenefitConsumptionStatus.REJECTED),
            (PayrollStatus.REJECTED, BenefitConsumptionStatus.DUPLICATE),
            (PayrollStatus.RECONCILED, BenefitConsumptionStatus.RECONCILED),
        )
        for payroll_status, status in cases:
            with self.subTest(payroll_status=payroll_status, status=status):
                payroll = self._payroll(payroll_status)
                benefit = self._request_deletion(self._benefit(status, payroll))
                self.assertEqual(restore_benefit_after_refused_deletion(benefit, self.user), status)
                benefit.refresh_from_db()
                self.assertEqual(benefit.status, status)
                self.assertNotIn('pending_deletion', benefit.json_ext)

    def test_a_payable_status_does_not_come_back_in_a_payroll_that_no_longer_pays(self):
        for payroll_status in (PayrollStatus.REJECTED, PayrollStatus.FAILED, PayrollStatus.RECONCILED):
            for status in (BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT):
                with self.subTest(payroll_status=payroll_status, status=status):
                    payroll = self._payroll(payroll_status)
                    benefit = self._request_deletion(self._benefit(status, payroll))
                    with self.assertLogs('payroll.services', level='ERROR'):
                        self.assertIsNone(restore_benefit_after_refused_deletion(benefit, self.user))
                    benefit.refresh_from_db()
                    self.assertEqual(benefit.status, BenefitConsumptionStatus.PENDING_DELETION)

        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        benefit = self._request_deletion(self._benefit(BenefitConsumptionStatus.ACCEPTED, payroll))
        Payroll.objects.filter(id=payroll.id).update(is_deleted=True)
        with self.assertLogs('payroll.services', level='ERROR'):
            self.assertIsNone(restore_benefit_after_refused_deletion(benefit, self.user))

    def test_a_request_made_before_the_marker_existed_reads_the_history(self):
        benefit = self._benefit(BenefitConsumptionStatus.REJECTED)
        benefit.status = BenefitConsumptionStatus.PENDING_DELETION
        benefit.save(username=self.user.username)

        restore_benefit_after_refused_deletion(benefit, self.user)

        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.REJECTED)

    def test_an_unknown_previous_status_leaves_the_benefit_pending_deletion(self):
        benefit = self._benefit(BenefitConsumptionStatus.PENDING_DELETION)
        with self.assertLogs('payroll.services', level='ERROR'):
            self.assertIsNone(restore_benefit_after_refused_deletion(benefit, self.user))
        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.PENDING_DELETION)

    def test_the_refused_deletion_task_restores_the_status(self):
        """End to end through ``task_service.complete_task``: the checker
        refuses the deletion of a benefit already sent to the agency."""
        from tasks_management.models import Task
        from tasks_management.services import TaskService

        benefit = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
                                self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT))
        BenefitConsumptionService(self.user).delete({'id': benefit.id})
        task = Task.objects.get(entity_id=str(benefit.id),
                                business_event=PayrollConfig.benefit_delete_event)

        TaskService(self.user).complete_task({'id': task.id, 'failed': True})

        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)


class CsvReconciliationKeepsJsonExtTest(_Fixtures):

    def test_the_reconciled_benefit_keeps_its_json_ext(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        before = {
            'fee_amount': 1440.0, 'total_with_fee': 73440.0, 'phoneNumber': '79000000',
            'payment_provider': {'transaction_reference': 'TRX-1'},
            'extra_info': {'agence': 'Gitega'},
        }
        benefit = self._benefit(BenefitConsumptionStatus.ACCEPTED, payroll, json_ext=before)
        row = pd.Series({
            'code': benefit.code, 'status': benefit.status,
            PayrollConfig.csv_reconciliation_receipt_column: 'RCPT-1',
            PayrollConfig.csv_reconciliation_paid_extra_field: PayrollConfig.csv_reconciliation_paid_yes,
            'Guichet': 'G-12',
        })

        CsvReconciliationService(self.user)._reconcile_bc(row, benefit)

        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.RECONCILED)
        self.assertEqual(benefit.receipt, 'RCPT-1')
        for key in ('fee_amount', 'total_with_fee', 'phoneNumber', 'payment_provider'):
            self.assertEqual(benefit.json_ext[key], before[key])
        self.assertEqual(benefit.json_ext['extra_info']['agence'], 'Gitega')
        self.assertEqual(benefit.json_ext['extra_info']['Guichet'], 'G-12')
