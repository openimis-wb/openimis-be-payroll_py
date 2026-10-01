"""Guards on the paths that send, restore or reconcile a benefit.

- The gateway task sends a payroll only while it is APPROVE_FOR_PAYMENT.
- A refused benefit deletion gives the benefit back the status it had, not
  ACCEPTED.
- A CSV reconciliation adds its columns to the benefit's json_ext and keeps
  the keys already there.
- No payroll is built by moving another payroll's benefits into it.
- Rejecting an approved payroll never takes back a payment: a benefit sent,
  reconciled or receipted keeps its status and receipt.
- A benefit the gateway accepted whose save fails is logged at error level,
  marked ``unpersisted_push`` in a separate write and returned; the push
  goes on and the gateway task returns what the strategy reports.
- Rejecting a payroll at approval keeps live and holds what an agency may
  have paid; the deletion of a payroll holding such a benefit is refused.
- The base online strategy reads a connector's dict result by its
  ``success``, keeps a receipt the benefit holds, and logs a failed
  reconciliation save at error level.
"""
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase, TestCase

from core.signals import REGISTERED_SERVICE_SIGNALS
from core.test_helpers import LogInHelper
from individual.models import Individual
from payroll.apps import PayrollConfig
from payroll.models import (
    BenefitConsumption, BenefitConsumptionStatus, Payroll, PayrollBenefitConsumption,
    PayrollStatus,
)
from payroll.services import BenefitConsumptionService, CsvReconciliationService, PayrollService
from payroll.strategies import StrategyOfPaymentInterface, StrategyOnlinePayment
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

    def test_the_task_returns_what_the_strategy_reports(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        strategy = mock.MagicMock()
        strategy.make_payment_for_payroll.return_value = {'persist_failed': []}
        with mock.patch('payroll.tasks.PaymentMethodStorage.get_chosen_payment_method',
                        return_value=strategy):
            result = send_requests_to_gateway_payment(str(payroll.id), str(self.user.id))
        self.assertEqual(result, {'persist_failed': []})


class ApprovedPushSaveFailureTest(_Fixtures):

    def test_a_failed_save_is_logged_recorded_and_returned(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        first, lost, last = (self._benefit(BenefitConsumptionStatus.ACCEPTED, payroll)
                             for _ in range(3))
        real_save = BenefitConsumption.save

        def save(benefit, *args, **kwargs):
            if benefit.id == lost.id:
                raise RuntimeError('database unavailable')
            return real_save(benefit, *args, **kwargs)

        with mock.patch.object(BenefitConsumption, 'save', autospec=True, side_effect=save), \
                self.assertLogs('payroll.strategies.strategy_online_payment', 'ERROR') as logs:
            failures = StrategyOnlinePayment.approve_for_payment_benefit_consumption(
                [first, lost, last], self.user)

        self.assertEqual(failures, [{'benefit_id': str(lost.id), 'code': lost.code,
                                     'error': 'database unavailable', 'response_recorded': True}])
        self.assertTrue(any(str(lost.id) in line for line in logs.output))
        for benefit in (first, last):
            benefit.refresh_from_db()
            self.assertEqual(benefit.status, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)
        lost.refresh_from_db()
        self.assertEqual(lost.status, BenefitConsumptionStatus.ACCEPTED)
        self.assertEqual(lost.json_ext['unpersisted_push']['error'], 'database unavailable')


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


class _RowsFixtures(_Fixtures):
    """A payroll holding a reconciled benefit, one approved for payment, one
    with a receipt and one waiting, each with a bill, a bill item and an
    attachment."""

    def _billed(self, status, payroll, receipt=None):
        from invoice.models import Bill, BillItem
        from payroll.models import BenefitAttachment

        benefit = self._benefit(status, payroll)
        if receipt:
            BenefitConsumption.objects.filter(id=benefit.id).update(receipt=receipt)
        bill = Bill(code=f'B-{uuid.uuid4().hex[:8]}', amount_total=72000, amount_net=72000)
        bill.save(username=self.user.username)
        BillItem(bill=bill, code=f'BI-{uuid.uuid4().hex[:8]}', amount_total=72000).save(
            username=self.user.username)
        BenefitAttachment(benefit=benefit, bill=bill).save(username=self.user.username)
        return BenefitConsumption.objects.get(id=benefit.id), bill

    def _rows(self, payroll):
        self.reconciled, self.reconciled_bill = self._billed(
            BenefitConsumptionStatus.RECONCILED, payroll, receipt='RCPT-1')
        self.sent, _ = self._billed(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll)
        self.receipted, _ = self._billed(BenefitConsumptionStatus.ACCEPTED, payroll, receipt='IBB-2')
        self.waiting, self.waiting_bill = self._billed(BenefitConsumptionStatus.ACCEPTED, payroll)

    def assertKept(self, benefit, status, receipt, stage):
        from payroll.models import BenefitAttachment
        row = BenefitConsumption.objects.get(id=benefit.id)
        self.assertEqual((row.is_deleted, row.status, row.receipt), (False, status, receipt))
        self.assertEqual(row.json_ext['rejection_hold']['stage'], stage)
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            benefit_id=benefit.id, is_deleted=False).exists())
        self.assertTrue(BenefitAttachment.objects.filter(
            benefit_id=benefit.id, is_deleted=False, bill__is_deleted=False).exists())

    def assertRemoved(self, benefit, bill):
        from invoice.models import Bill, BillItem
        from payroll.models import BenefitAttachment
        self.assertFalse(BenefitConsumption.objects.filter(id=benefit.id).exists())
        self.assertTrue(BenefitConsumption.history.filter(id=benefit.id, history_type='-').exists())
        self.assertFalse(PayrollBenefitConsumption.objects.filter(benefit_id=benefit.id).exists())
        self.assertFalse(BenefitAttachment.objects.filter(benefit_id=benefit.id).exists())
        self.assertFalse(Bill.objects.filter(id=bill.id).exists())
        self.assertFalse(BillItem.objects.filter(bill_id=bill.id).exists())


class RejectedPayrollKeepsRowsTest(_RowsFixtures):
    """Rejecting a payroll at approval removes only what no agency may have
    paid: the rest stays live and held."""

    def test_an_offline_payroll_rejected_at_approval_keeps_its_rows(self):
        from payroll.strategies import StrategyOfflinePayment

        payroll = self._payroll(PayrollStatus.PENDING_APPROVAL)
        self._rows(payroll)

        StrategyOfflinePayment.reject_payroll(payroll, self.user, task_id='T-9')

        payroll.refresh_from_db()
        self.assertEqual(payroll.status, PayrollStatus.REJECTED)
        self.assertKept(self.reconciled, BenefitConsumptionStatus.RECONCILED, 'RCPT-1', 'payroll_rejected')
        self.assertKept(self.sent, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, None, 'payroll_rejected')
        self.assertKept(self.receipted, BenefitConsumptionStatus.ACCEPTED, 'IBB-2', 'payroll_rejected')
        hold = BenefitConsumption.objects.get(id=self.reconciled.id).json_ext['rejection_hold']
        self.assertEqual((hold['task_id'], hold['payroll_id'], hold['reason']),
                         ('T-9', str(payroll.id), 'status RECONCILED'))
        self.assertRemoved(self.waiting, self.waiting_bill)


class PayrollDeletionTest(_RowsFixtures):
    """An approved payroll deletion is refused for a payroll that holds a
    benefit an agency may have paid."""

    def _delete_through_task(self, payroll, status_at_completion=None):
        """Request the deletion, then complete its task; with
        ``status_at_completion``, the payroll reaches that status between
        the request and the completion."""
        from payroll.strategies import StrategyOfflinePayment
        from tasks_management.models import Task
        from tasks_management.services import TaskService

        PayrollService(self.user).delete({'id': payroll.id})
        task = Task.objects.get(entity_id=str(payroll.id),
                                business_event=PayrollConfig.payroll_delete_event)
        if status_at_completion:
            Payroll.objects.filter(id=payroll.id).update(status=status_at_completion)
        with mock.patch('payroll.signals.PaymentMethodStorage.get_chosen_payment_method',
                        return_value=StrategyOfflinePayment):
            TaskService(self.user).complete_task({'id': task.id})

    def test_a_payroll_nothing_was_paid_from_is_deleted(self):
        payroll = self._payroll(PayrollStatus.PENDING_APPROVAL)
        waiting, bill = self._billed(BenefitConsumptionStatus.ACCEPTED, payroll)

        self._delete_through_task(payroll)

        self.assertTrue(Payroll.objects.get(id=payroll.id).is_deleted)
        self.assertRemoved(waiting, bill)

    def test_a_payroll_holding_a_paid_benefit_is_not_deleted(self):
        payroll = self._payroll(PayrollStatus.PENDING_APPROVAL)
        self._rows(payroll)

        with self.assertLogs('payroll.strategies', level='ERROR'):
            self._delete_through_task(payroll, status_at_completion=PayrollStatus.APPROVE_FOR_PAYMENT)

        self.assertFalse(Payroll.objects.get(id=payroll.id).is_deleted)
        for benefit in (self.reconciled, self.sent, self.receipted, self.waiting):
            row = BenefitConsumption.objects.get(id=benefit.id)
            self.assertEqual((row.is_deleted, row.status), (False, benefit.status))
            self.assertNotIn('rejection_hold', row.json_ext)
        self.assertFalse(PayrollBenefitConsumption.objects.filter(
            payroll=payroll, is_deleted=True).exists())

    def test_a_benefit_held_at_removal_stops_the_deletion(self):
        """The removal's own reading of the rows decides: a benefit it finds
        possibly paid stops the deletion, whatever an earlier reading said."""
        import sys
        from payroll.strategies import StrategyOfflinePayment

        payroll = self._payroll(PayrollStatus.PENDING_APPROVAL)
        waiting, _ = self._billed(BenefitConsumptionStatus.ACCEPTED, payroll)
        other, _ = self._billed(BenefitConsumptionStatus.ACCEPTED, payroll)

        def reasons(cls, benefits):
            if sys._getframe(1).f_code.co_name != 'remove_benefits_from_rejected_payroll':
                return {}
            return {benefit.id: 'push attempt' for benefit in benefits if benefit.id == waiting.id}

        with mock.patch.object(StrategyOfflinePayment, 'sent_benefit_reasons', classmethod(reasons)), \
                self.assertLogs('payroll.strategies', level='ERROR'):
            deleted = StrategyOfflinePayment.delete_payroll(payroll, self.user)

        self.assertFalse(deleted)
        self.assertFalse(Payroll.objects.get(id=payroll.id).is_deleted)
        for benefit in (waiting, other):
            row = BenefitConsumption.objects.get(id=benefit.id)
            self.assertEqual((row.is_deleted, row.status), (False, BenefitConsumptionStatus.ACCEPTED))
            self.assertTrue(PayrollBenefitConsumption.objects.filter(
                payroll=payroll, benefit_id=benefit.id, is_deleted=False).exists())


class PayrollDeletionRequestTest(_RowsFixtures):
    """A payroll an agency may have been paid from (APPROVE_FOR_PAYMENT and
    any status but GENERATING, PENDING_VERIFICATION, PENDING_APPROVAL and
    FAILED) is not put up for deletion unless none of its benefits may have
    been paid."""

    def _deletion_tasks(self, payroll):
        from tasks_management.models import Task
        return Task.objects.filter(entity_id=str(payroll.id),
                                   business_event=PayrollConfig.payroll_delete_event)

    def test_a_payroll_holding_a_sent_benefit_is_refused_once_approved(self):
        for status in (PayrollStatus.APPROVE_FOR_PAYMENT, PayrollStatus.RECONCILED,
                       PayrollStatus.REJECTED):
            with self.subTest(payroll_status=status):
                payroll = self._payroll(status)
                self._billed(BenefitConsumptionStatus.ACCEPTED, payroll)
                self._billed(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll)
                with self.assertRaises(ValueError):
                    PayrollService(self.user).delete({'id': payroll.id})
                self.assertFalse(self._deletion_tasks(payroll).exists())

    def test_an_approved_payroll_nothing_was_sent_from_is_put_up_for_deletion(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        self._billed(BenefitConsumptionStatus.ACCEPTED, payroll)
        PayrollService(self.user).delete({'id': payroll.id})
        self.assertEqual(self._deletion_tasks(payroll).count(), 1)

    def test_a_payroll_not_yet_approved_is_put_up_for_deletion(self):
        for status in (PayrollStatus.PENDING_APPROVAL, 'PENDING_VERIFICATION',
                       PayrollStatus.GENERATING, PayrollStatus.FAILED):
            with self.subTest(payroll_status=status):
                payroll = self._payroll(status)
                self._billed(BenefitConsumptionStatus.ACCEPTED, payroll)
                PayrollService(self.user).delete({'id': payroll.id})
                self.assertEqual(self._deletion_tasks(payroll).count(), 1)


class OnlineSendResultTest(_Fixtures):
    """The base online strategy reads a connector's dict result by its
    ``success``: a refused send is not marked APPROVE_FOR_PAYMENT."""

    def _send(self, result):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        benefit = self._benefit(BenefitConsumptionStatus.ACCEPTED, payroll)
        connector = mock.MagicMock()
        connector.send_payment.return_value = result
        with mock.patch.object(StrategyOnlinePayment, 'PAYMENT_GATEWAY', connector):
            summary = StrategyOnlinePayment.make_payment_for_payroll(payroll, self.user)
        benefit.refresh_from_db()
        return benefit, summary

    def test_a_refused_send_stays_accepted(self):
        for result in ({'success': False, 'data': {'statusCode': '65200'}, 'error': 'declined'},
                       {'success': False, 'data': None, 'error': 'timeout'}, False):
            with self.subTest(result=result):
                benefit, summary = self._send(result)
                self.assertEqual(benefit.status, BenefitConsumptionStatus.ACCEPTED)
                self.assertEqual(summary['succeeded'], 0)

    def test_an_accepted_send_is_approved_for_payment(self):
        for result in ({'success': True, 'data': {'statusCode': '200'}, 'error': None}, True):
            with self.subTest(result=result):
                benefit, summary = self._send(result)
                self.assertEqual(benefit.status, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)
                self.assertEqual(summary['succeeded'], 1)


class OnlineReconcileTest(_Fixtures):

    def test_a_reconciled_benefit_keeps_its_receipt(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        with_receipt = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll)
        without = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll)
        BenefitConsumption.objects.filter(id=with_receipt.id).update(receipt='IBB-77')

        StrategyOnlinePayment.reconcile_benefit_consumption(
            list(BenefitConsumption.objects.filter(id__in=[with_receipt.id, without.id])), self.user)

        with_receipt.refresh_from_db()
        without.refresh_from_db()
        self.assertEqual((with_receipt.status, with_receipt.receipt),
                         (BenefitConsumptionStatus.RECONCILED, 'IBB-77'))
        self.assertEqual(without.status, BenefitConsumptionStatus.RECONCILED)
        self.assertTrue(without.receipt)

    def test_a_failed_save_is_logged_at_error_level(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        benefit = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll)

        with mock.patch.object(BenefitConsumption, 'save', side_effect=RuntimeError('database unavailable')), \
                self.assertLogs('payroll.strategies.strategy_online_payment', 'ERROR') as logs:
            StrategyOnlinePayment.reconcile_benefit_consumption([benefit], self.user)

        self.assertTrue(any(benefit.code in line for line in logs.output))


class _TestPullStrategy(StrategyOnlinePayment):
    """An online strategy whose agency pulls its list: no gateway."""

    @classmethod
    def initialize_payment_gateway(cls, payment_point=None):
        cls.PAYMENT_GATEWAY = None


class ReconcileTaskStrategyTest(_Fixtures):
    """The closing task runs the payroll's own strategy: a strategy without a
    gateway closes the payroll without asking any gateway what it paid."""

    def _close(self, payroll, strategy):
        from payroll.tasks import send_request_to_reconcile
        with mock.patch('payroll.tasks.PaymentMethodStorage.get_chosen_payment_method',
                        return_value=strategy), \
                mock.patch('payroll.payment_gateway.PaymentGatewayConfig') as config:
            send_request_to_reconcile(str(payroll.id), str(self.user.id))
        return config

    def test_a_payroll_without_a_gateway_is_closed_without_a_lookup(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        benefit = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll,
                                json_ext={'payment_provider': {'acknowledgment_status': 'ACCEPTED'}})

        config = self._close(payroll, _TestPullStrategy)

        config.assert_not_called()
        self.assertEqual(Payroll.objects.get(id=payroll.id).status, PayrollStatus.RECONCILED)
        row = BenefitConsumption.objects.get(id=benefit.id)
        self.assertEqual((row.status, row.receipt, row.json_ext),
                         (BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, None,
                          {'payment_provider': {'acknowledgment_status': 'ACCEPTED'}}))

    def test_a_payroll_with_a_gateway_is_looked_up_through_its_strategy(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        benefit = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll)
        connector = mock.MagicMock()
        connector.reconcile.return_value = True

        class Strategy(StrategyOnlinePayment):
            @classmethod
            def initialize_payment_gateway(cls, payment_point=None):
                cls.PAYMENT_GATEWAY = connector

        config = self._close(payroll, Strategy)

        config.assert_not_called()
        connector.reconcile.assert_called_once_with(benefit.code, benefit.amount)
        self.assertEqual(Payroll.objects.get(id=payroll.id).status, PayrollStatus.RECONCILED)
        self.assertEqual(BenefitConsumption.objects.get(id=benefit.id).status,
                         BenefitConsumptionStatus.RECONCILED)

    def test_a_payroll_without_a_registered_strategy_is_left_as_it_is(self):
        payroll = self._payroll(PayrollStatus.APPROVE_FOR_PAYMENT)
        benefit = self._benefit(BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, payroll)

        with self.assertLogs('payroll.tasks', level='ERROR'):
            config = self._close(payroll, None)

        config.assert_not_called()
        self.assertEqual(Payroll.objects.get(id=payroll.id).status, PayrollStatus.APPROVE_FOR_PAYMENT)
        self.assertEqual(BenefitConsumption.objects.get(id=benefit.id).status,
                         BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)


class PayrollDeletionRaceTest(SimpleTestCase):
    """The deletion of a payroll and a send of its benefits, in two committed
    transactions. A sender claims its rows with ``SKIP LOCKED`` and marks
    them sent (APPROVE_FOR_PAYMENT) before it commits: the deletion either
    removes rows no sender can take any more, or finds them sent and deletes
    nothing.

    Runs outside a test transaction, so each thread sees the other's
    commits; the rows it creates are removed in ``tearDown``.
    """
    databases = {'default'}
    WAIT = 20

    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api(username='payroll_race_user')
        self.individual = Individual(first_name='Course', last_name='Paiement', dob='1990-01-01')
        self.individual.save(username=self.user.username)
        self.payroll = Payroll(name=f'RACE-{uuid.uuid4().hex[:6]}', status=PayrollStatus.APPROVE_FOR_PAYMENT,
                               payment_method='StrategyGuardTest', json_ext={})
        self.payroll.save(username=self.user.username)
        self.benefits = []
        for _ in range(3):
            benefit = BenefitConsumption(
                individual=self.individual, code=f'RACE-{uuid.uuid4().hex[:8]}', amount=72000,
                type='Cash Transfer', status=BenefitConsumptionStatus.ACCEPTED,
                date_due=date(2026, 10, 1), json_ext={})
            benefit.save(username=self.user.username)
            PayrollBenefitConsumption(payroll=self.payroll, benefit=benefit).save(
                username=self.user.username)
            self.benefits.append(benefit)
        self.ids = [benefit.id for benefit in self.benefits]

    def tearDown(self):
        PayrollBenefitConsumption.objects.filter(payroll_id=self.payroll.id).delete()
        BenefitConsumption.objects.filter(id__in=self.ids).delete()
        Payroll.objects.filter(id=self.payroll.id).delete()
        Individual.objects.filter(id=self.individual.id).delete()

    def _claim(self, on_locked=None):
        """A sender's claim: lock the ACCEPTED rows it can take, mark them
        sent, commit. Returns the ids it took."""
        from django.db import transaction
        with transaction.atomic():
            rows = list(BenefitConsumption.objects.select_for_update(skip_locked=True)
                        .filter(id__in=self.ids, status=BenefitConsumptionStatus.ACCEPTED,
                                is_deleted=False).order_by('id'))
            if on_locked:
                on_locked()
            BenefitConsumption.objects.filter(id__in=[row.id for row in rows]).update(
                status=BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)
        return [row.id for row in rows]

    def _in_thread(self, target, result, started=None):
        import threading
        from django.db import connection

        def run():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '%ss'" % self.WAIT)
                    cursor.execute('SELECT pg_backend_pid()')
                    result['pid'] = cursor.fetchone()[0]
                if started:
                    started.set()
                result['value'] = target()
            except Exception as error:
                result['error'] = error
            finally:
                connection.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _wait_for_lock(self, pid):
        """True once the backend ``pid`` waits on a lock."""
        import time
        from django.db import connection
        deadline = time.monotonic() + self.WAIT
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s', [pid])
                row = cursor.fetchone()
            if row and row[0] == 'Lock':
                return True
            time.sleep(0.05)
        return False

    def test_a_claim_during_the_deletion_takes_nothing(self):
        """The sender claims while the deletion has read the rows and not yet
        deleted them: it takes none, and the deletion removes them all."""
        import sys
        import threading
        from payroll.strategies import StrategyOfflinePayment

        checked, resume = threading.Event(), threading.Event()
        real = StrategyOfflinePayment.sent_benefit_reasons.__func__

        def reasons(cls, benefits):
            found = real(cls, benefits)
            if sys._getframe(1).f_code.co_name == 'remove_benefits_from_rejected_payroll':
                checked.set()
                resume.wait(self.WAIT)
            return found

        deletion = {}
        with mock.patch.object(StrategyOfflinePayment, 'sent_benefit_reasons', classmethod(reasons)):
            thread = self._in_thread(
                lambda: StrategyOfflinePayment.delete_payroll(self.payroll, self.user), deletion)
            self.assertTrue(checked.wait(self.WAIT), 'the deletion never read its rows')
            try:
                claimed = self._claim()
            finally:
                resume.set()
                thread.join(self.WAIT)

        self.assertNotIn('error', deletion)
        self.assertEqual(claimed, [])
        self.assertTrue(deletion['value'])
        self.assertFalse(BenefitConsumption.objects.filter(id__in=self.ids).exists())
        self.assertTrue(Payroll.objects.get(id=self.payroll.id).is_deleted)

    def test_a_deletion_during_a_claim_waits_and_deletes_nothing(self):
        """The deletion starts while a sender holds the rows: it waits for the
        sender's commit, then finds them sent and deletes nothing."""
        import threading
        from payroll.strategies import StrategyOfflinePayment

        locked, release, started = threading.Event(), threading.Event(), threading.Event()

        def on_locked():
            locked.set()
            release.wait(self.WAIT)

        claim, deletion = {}, {}
        claimer = self._in_thread(lambda: self._claim(on_locked), claim)
        self.assertTrue(locked.wait(self.WAIT), 'the sender never locked its rows')
        deleter = self._in_thread(
            lambda: StrategyOfflinePayment.delete_payroll(self.payroll, self.user), deletion, started)
        try:
            waited = started.wait(self.WAIT) and self._wait_for_lock(deletion['pid'])
        finally:
            release.set()
            claimer.join(self.WAIT)
            deleter.join(self.WAIT)

        self.assertNotIn('error', claim)
        self.assertNotIn('error', deletion)
        self.assertTrue(waited, 'the deletion never waited for the sender')
        self.assertEqual(sorted(claim['value']), sorted(self.ids))
        self.assertFalse(deletion['value'])
        self.assertEqual(
            sorted(BenefitConsumption.objects.filter(id__in=self.ids).values_list('status', flat=True)),
            [BenefitConsumptionStatus.APPROVE_FOR_PAYMENT] * 3)
        self.assertFalse(Payroll.objects.get(id=self.payroll.id).is_deleted)
