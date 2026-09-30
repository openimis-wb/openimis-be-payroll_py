"""The reconciliation request is queued once the resolving transaction commits.

``task_service.complete_task`` runs its AFTER receivers inside the transaction
of ``resolve_task``. The payroll receiver for the reconciliation event hands
the payroll to the Celery task, which calls the payment gateway and sets the
payroll RECONCILED. Queued before the commit, the task also runs when the
resolution rolls back, and its effects outlive the rolled-back task status.
"""
import uuid
from unittest import mock

from django.db import transaction
from django.test import TestCase

from core.test_helpers import LogInHelper
from payroll.apps import PayrollConfig
from payroll.models import Payroll, PayrollStatus
from payroll.tasks import send_request_to_reconcile
from tasks_management.models import Task
from tasks_management.services import TaskService


class _ResolutionAborted(Exception):
    pass


class ReconcileAfterCommitTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username='payroll_reconcile_user')

    def _reconciliation_task(self, payment_method='StrategyOnlinePayment'):
        payroll = Payroll(name=f'P-{uuid.uuid4().hex[:6]}', status=PayrollStatus.APPROVE_FOR_PAYMENT,
                          payment_method=payment_method, json_ext={})
        payroll.save(username=self.user.username)
        task = Task(source='payroll_reconciliation', entity=payroll, status=Task.Status.ACCEPTED,
                    business_event=PayrollConfig.payroll_reconciliation_event)
        task.save(username=self.user.username)
        return payroll, task

    def _complete(self, task):
        result = TaskService(self.user).complete_task({'id': task.id})
        self.assertTrue(result['success'], result)

    def test_the_request_is_queued_only_after_the_commit(self):
        payroll, task = self._reconciliation_task()
        with mock.patch.object(send_request_to_reconcile, 'delay') as delay:
            with self.captureOnCommitCallbacks() as callbacks:
                with transaction.atomic():
                    self._complete(task)
                    delay.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            delay.assert_not_called()

            for callback in callbacks:
                callback()
            delay.assert_called_once_with(payroll.id, self.user.id)

    def test_nothing_is_queued_when_the_resolution_rolls_back(self):
        _, task = self._reconciliation_task()
        with mock.patch.object(send_request_to_reconcile, 'delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(_ResolutionAborted):
                    with transaction.atomic():
                        self._complete(task)
                        raise _ResolutionAborted()
            delay.assert_not_called()

    def test_a_payroll_without_a_registered_strategy_queues_nothing(self):
        _, task = self._reconciliation_task(payment_method='StrategyNotRegistered')
        with mock.patch.object(send_request_to_reconcile, 'delay') as delay:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with transaction.atomic():
                    self._complete(task)
            self.assertEqual(callbacks, [])
            delay.assert_not_called()
