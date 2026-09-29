"""Guards on the paths that send, restore or reconcile a benefit.

- The gateway task sends a payroll only while it is APPROVE_FOR_PAYMENT.
- A refused benefit deletion gives the benefit back the status it had, not
  ACCEPTED.
- A CSV reconciliation adds its columns to the benefit's json_ext and keeps
  the keys already there.
- No payroll is built by moving another payroll's benefits into it.
- Rejecting an approved payroll never takes back a payment: a benefit sent,
  reconciled or receipted keeps its status and receipt.
"""
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date
from unittest import mock

import pandas as pd
from django.test import TestCase

from core.signals import REGISTERED_SERVICE_SIGNALS
from core.test_helpers import LogInHelper
from individual.models import Individual
from payroll.apps import PayrollConfig
from payroll.models import (
    BenefitConsumption, BenefitConsumptionStatus, Payroll, PayrollBenefitConsumption,
    PayrollStatus,
)
from payroll.services import BenefitConsumptionService, CsvReconciliationService, PayrollService
from payroll.strategies import StrategyOfPaymentInterface
from payroll.tasks import send_requests_to_gateway_payment


@contextmanager
def _without_other_modules(*signal_names):
    """Run the payroll services without the receivers other installed modules
    bind before them, so each test reads this module's own behaviour."""
    with ExitStack() as stack:
        for name in signal_names:
            signal = REGISTERED_SERVICE_SIGNALS[name].before_service_signal
            stack.enter_context(mock.patch.object(signal, 'receivers', []))
        yield


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

    def test_a_payable_status_comes_back_in_a_live_payroll_not_yet_closed(self):
        for payroll_status in ('PENDING_VERIFICATION', PayrollStatus.PENDING_APPROVAL,
                               PayrollStatus.APPROVE_FOR_PAYMENT):
            for status in (BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT):
                with self.subTest(payroll_status=payroll_status, status=status):
                    payroll = self._payroll(payroll_status)
                    benefit = self._request_deletion(self._benefit(status, payroll))
                    self.assertEqual(restore_benefit_after_refused_deletion(benefit, self.user), status)
                    benefit.refresh_from_db()
                    self.assertEqual(benefit.status, status)
                    self.assertNotIn('pending_deletion', benefit.json_ext)

    def test_a_payable_status_stays_pending_deletion_in_a_dead_or_closed_payroll(self):
        for payroll_status in (PayrollStatus.REJECTED, PayrollStatus.FAILED, PayrollStatus.RECONCILED):
            for status in (BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT):
                with self.subTest(payroll_status=payroll_status, status=status):
                    payroll = self._payroll(payroll_status)
                    benefit = self._request_deletion(self._benefit(status, payroll))
                    with self.assertLogs('payroll.services', level='ERROR'):
                        self.assertIsNone(restore_benefit_after_refused_deletion(benefit, self.user))
                    benefit.refresh_from_db()
                    self.assertEqual(benefit.status, BenefitConsumptionStatus.PENDING_DELETION)

        for payroll_status in (PayrollStatus.APPROVE_FOR_PAYMENT, PayrollStatus.PENDING_APPROVAL):
            with self.subTest(deleted_payroll=payroll_status):
                payroll = self._payroll(payroll_status)
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


class MovedBenefitsRefusedTest(_Fixtures):
    """A payroll built from another payroll's benefits would take benefits
    already sent to the agency and send them again: every entry is refused."""

    def setUp(self):
        self.source = self._payroll(PayrollStatus.RECONCILED)
        self.sent = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, self.source)
        self.waiting = self._benefit(BenefitConsumptionStatus.ACCEPTED, self.source)

    def _assert_source_untouched(self):
        for benefit, status in ((self.sent, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT),
                                (self.waiting, BenefitConsumptionStatus.ACCEPTED)):
            benefit.refresh_from_db()
            self.assertEqual(benefit.status, status)
            self.assertEqual(
                list(PayrollBenefitConsumption.objects.filter(benefit=benefit, is_deleted=False)
                     .values_list('payroll_id', flat=True)),
                [self.source.id])

    def test_creation_from_another_payroll_is_refused(self):
        with _without_other_modules('payroll_service.create'), \
                mock.patch('payroll.services.create_payroll_benefits_task') as task:
            result = PayrollService(self.user).create({
                'name': 'Retry', 'payment_plan_id': uuid.uuid4(),
                'payment_method': 'StrategyGuardTest',
                'from_failed_invoices_payroll_id': self.source.id,
            })
        self.assertFalse(result['success'])
        self.assertIn('recreate_payroll_benefits', result['detail'])
        task.delay.assert_not_called()
        self.assertFalse(Payroll.objects.filter(name='Retry').exists())
        self._assert_source_untouched()

    def test_generation_from_another_payroll_is_refused(self):
        """A creation queued with the moving parameter fails its payroll and
        moves nothing."""
        payroll = self._payroll(PayrollStatus.GENERATING)
        service = PayrollService(self.user)
        with mock.patch.object(PayrollService, '_get_payment_plan'), \
                mock.patch.object(PayrollService, '_get_payment_cycle'), \
                mock.patch.object(PayrollService, 'create_accept_payroll_task') as accept, \
                self.assertLogs('payroll.services', level='ERROR'):
            with self.assertRaises(ValueError):
                service._create_payroll_benefits(
                    payroll, {'from_failed_invoices_payroll_id': str(self.source.id)})
        accept.assert_not_called()
        payroll.refresh_from_db()
        self.assertEqual(payroll.status, PayrollStatus.FAILED)
        self.assertIn('recreate_payroll_benefits', payroll.json_ext['creation_error'])
        self._assert_source_untouched()

    def test_a_retrigger_of_a_payroll_built_from_another_is_refused(self):
        payroll = self._payroll(PayrollStatus.FAILED)
        Payroll.objects.filter(id=payroll.id).update(json_ext={'creation_params': {
            'name': 'Retry', 'from_failed_invoices_payroll_id': str(self.source.id)}})
        with _without_other_modules('payroll_service.retrigger_creation'), \
                mock.patch('payroll.services.create_payroll_benefits_task') as task:
            result = PayrollService(self.user).retrigger_creation({'id': payroll.id})
        self.assertFalse(result['success'])
        self.assertIn('recreate_payroll_benefits', result['detail'])
        task.delay.assert_not_called()
        payroll.refresh_from_db()
        self.assertEqual(payroll.status, PayrollStatus.FAILED)
        self._assert_source_untouched()


class RejectApprovedPayrollTest(_Fixtures):
    """Rejecting an approved payroll sends it back to approval without taking
    back what was sent: a benefit reconciled, approved for payment or holding
    a receipt keeps its status and receipt, and the rejection is recorded on
    it."""

    def setUp(self):
        self.payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        self.reconciled = self._benefit(BenefitConsumptionStatus.RECONCILED, self.payroll)
        self.sent = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, self.payroll)
        self.receipted = self._benefit(BenefitConsumptionStatus.ACCEPTED, self.payroll)
        self.waiting = self._benefit(BenefitConsumptionStatus.ACCEPTED, self.payroll)
        BenefitConsumption.objects.filter(id=self.reconciled.id).update(receipt='RCPT-1')
        BenefitConsumption.objects.filter(id=self.receipted.id).update(receipt='IBB-2')

    def _reject(self, payroll, **kwargs):
        with mock.patch.object(PayrollService, 'create_accept_payroll_task') as accept:
            StrategyOfPaymentInterface.reject_approved_payroll(payroll, self.user, **kwargs)
        return accept

    def test_what_was_sent_keeps_its_status_and_receipt(self):
        accept = self._reject(self.payroll, task_id='T-1')

        expected = (
            (self.reconciled, BenefitConsumptionStatus.RECONCILED, 'RCPT-1'),
            (self.sent, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, None),
            (self.receipted, BenefitConsumptionStatus.ACCEPTED, 'IBB-2'),
        )
        for benefit, status, receipt in expected:
            with self.subTest(status=status, receipt=receipt):
                benefit.refresh_from_db()
                self.assertEqual((benefit.status, benefit.receipt), (status, receipt))
                hold = benefit.json_ext['rejection_hold']
                self.assertEqual((hold['stage'], hold['task_id'], hold['payroll_id']),
                                 ('approved_payroll_rejected', 'T-1', str(self.payroll.id)))
        self.waiting.refresh_from_db()
        self.assertEqual(self.waiting.status, BenefitConsumptionStatus.ACCEPTED)
        self.assertNotIn('rejection_hold', self.waiting.json_ext)
        self.payroll.refresh_from_db()
        self.assertEqual(self.payroll.status, PayrollStatus.PENDING_APPROVAL)
        accept.assert_called_once()

    def test_only_a_live_approved_payroll_is_rejected(self):
        for status in (PayrollStatus.RECONCILED, PayrollStatus.REJECTED, PayrollStatus.PENDING_APPROVAL):
            with self.subTest(payroll_status=status):
                Payroll.objects.filter(id=self.payroll.id).update(status=status)
                with self.assertLogs('payroll.strategies', level='ERROR'):
                    accept = self._reject(Payroll.objects.get(id=self.payroll.id))
                accept.assert_not_called()
                self.assertEqual(Payroll.objects.get(id=self.payroll.id).status, status)
                self.reconciled.refresh_from_db()
                self.assertEqual((self.reconciled.status, self.reconciled.receipt),
                                 (BenefitConsumptionStatus.RECONCILED, 'RCPT-1'))
                self.assertNotIn('rejection_hold', self.reconciled.json_ext)

        Payroll.objects.filter(id=self.payroll.id).update(
            status=PayrollStatus.APPROVE_FOR_PAYMENT, is_deleted=True)
        with self.assertLogs('payroll.strategies', level='ERROR'):
            accept = self._reject(self.payroll)
        accept.assert_not_called()

    def test_the_service_refuses_a_payroll_that_is_not_approved(self):
        from tasks_management.models import Task

        def reject_tasks():
            return Task.objects.filter(entity_id=str(self.payroll.id),
                                       business_event=PayrollConfig.payroll_reject_event)

        Payroll.objects.filter(id=self.payroll.id).update(status=PayrollStatus.RECONCILED)
        with self.assertRaises(ValueError):
            PayrollService(self.user).reject_approved_payroll({'id': self.payroll.id})
        self.assertFalse(reject_tasks().exists())

        Payroll.objects.filter(id=self.payroll.id).update(status=PayrollStatus.APPROVE_FOR_PAYMENT)
        PayrollService(self.user).reject_approved_payroll({'id': self.payroll.id})
        self.assertEqual(reject_tasks().count(), 1)

    def test_the_completed_task_names_itself_to_the_strategy(self):
        from tasks_management.models import Task
        from tasks_management.services import TaskService

        PayrollService(self.user).reject_approved_payroll({'id': self.payroll.id})
        task = Task.objects.get(entity_id=str(self.payroll.id),
                                business_event=PayrollConfig.payroll_reject_event)
        strategy = mock.MagicMock()
        with mock.patch('payroll.signals.PaymentMethodStorage.get_chosen_payment_method',
                        return_value=strategy):
            TaskService(self.user).complete_task({'id': task.id})
        (payroll, user), kwargs = strategy.reject_approved_payroll.call_args
        self.assertEqual((payroll.id, list(kwargs)), (self.payroll.id, ['task_id']))
        self.assertEqual(str(kwargs['task_id']), str(task.id))
