import logging

from django.db.models import Q, Sum
from django.db import transaction

from core.signals import register_service_signal
from payroll.strategies.strategy_of_payments_interface import StrategyOfPaymentInterface
from payroll.utils import CodeGenerator

logger = logging.getLogger(__name__)

# json_ext key of a push the gateway accepted whose status could not be saved.
UNPERSISTED_PUSH_KEY = 'unpersisted_push'


def send_accepted(result):
    """True when a connector's ``send_payment`` result says the gateway took
    the payment: a boolean, or the ``success`` of a ``{'success', 'data',
    'error'}`` dict."""
    if isinstance(result, dict):
        return result.get('success') is True
    return bool(result)


class StrategyOnlinePayment(StrategyOfPaymentInterface):
    WORKFLOW_NAME = "payment-adaptor"
    WORKFLOW_GROUP = "openimis-coremis-payment-adaptor"
    PAYMENT_GATEWAY = None

    @classmethod
    def initialize_payment_gateway(cls, payment_point=None):
        from payroll.payment_gateway import PaymentGatewayConfig
        gateway_config = PaymentGatewayConfig(payment_point)
        payment_gateway_connector_class = gateway_config.get_payment_gateway_connector()
        cls.PAYMENT_GATEWAY = payment_gateway_connector_class(payment_point)

    @classmethod
    def accept_payroll(cls, payroll, user, **kwargs):
        cls._process_accepted_payroll(payroll, user, **kwargs)

    @classmethod
    def make_payment_for_payroll(cls, payroll, user, **kwargs):
        return cls._send_payment_data_to_gateway(payroll, user)

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        # save response coming from payment gateway in json_ext
        cls._save_payroll_data(payroll, user, response_from_gateway)

    @classmethod
    @transaction.atomic
    def reconcile_payroll(cls, payroll, user):
        from payroll.tasks import send_request_to_reconcile
        send_request_to_reconcile.delay(payroll.id, user.id)

    @classmethod
    def get_benefits_attached_to_payroll(cls, payroll, status):
        from payroll.models import BenefitConsumption
        filters = Q(
            payrollbenefitconsumption__payroll_id=payroll.id,
            is_deleted=False,
            status=status,
            payrollbenefitconsumption__is_deleted=False,
            payrollbenefitconsumption__payroll__is_deleted=False,
        )
        benefits = BenefitConsumption.objects.filter(filters)
        return benefits

    @classmethod
    def approve_for_payment_benefit_consumption(cls, benefits, user):
        """Mark benefits the gateway accepted as APPROVE_FOR_PAYMENT.

        A benefit whose save fails is logged at error level, gets
        ``json_ext.unpersisted_push`` in a separate write, and is returned;
        the other benefits are still saved. Returns the failures as
        ``[{'benefit_id', 'code', 'error', 'response_recorded'}]``.
        """
        from payroll.models import BenefitConsumptionStatus
        failures = []
        for benefit in benefits:
            try:
                with transaction.atomic():
                    benefit.status = BenefitConsumptionStatus.APPROVE_FOR_PAYMENT
                    benefit.save(username=user.login_name)
            except Exception as e:
                logger.exception("Push ACCEPTED by the gateway but not saved: benefit %s (code %s)",
                                 benefit.id, benefit.code)
                failures.append({'benefit_id': str(benefit.id), 'code': benefit.code,
                                 'error': str(e)[:500],
                                 'response_recorded': cls._record_unpersisted_push(benefit, e)})
        return failures

    @classmethod
    def _record_unpersisted_push(cls, benefit, error):
        """Write ``json_ext.unpersisted_push`` on a benefit whose accepted push
        was not saved, without history or the model's save; True when written."""
        from django.utils import timezone
        from payroll.models import BenefitConsumption
        json_ext = dict(benefit.json_ext) if isinstance(benefit.json_ext, dict) else {}
        json_ext[UNPERSISTED_PUSH_KEY] = {'at': timezone.now().isoformat(), 'reference': benefit.receipt,
                                          'error': str(error)[:500]}
        try:
            with transaction.atomic():
                BenefitConsumption.objects.filter(id=benefit.id).update(json_ext=json_ext)
            return True
        except Exception:
            logger.exception("Benefit %s (code %s): accepted push recorded nowhere", benefit.id, benefit.code)
            return False

    @classmethod
    def reconcile_benefit_consumption(cls, benefits, user):
        """Mark benefits the gateway confirmed as RECONCILED.

        A benefit keeps the receipt it already holds; one without a receipt
        gets a generated one. A benefit whose save fails is logged at error
        level and the others are still reconciled.
        """
        from payroll.models import BenefitConsumptionStatus
        from payroll.apps import PayrollConfig
        from invoice.models import Bill
        for benefit in benefits:
            try:
                if not benefit.receipt:
                    benefit.receipt = CodeGenerator.generate_unique_code(
                        'payroll',
                        'BenefitConsumption',
                        'receipt',
                        PayrollConfig.receipt_length,
                    )
                benefit.status = BenefitConsumptionStatus.RECONCILED
                benefit.save(username=user.login_name)
                bill = Bill.objects.filter(
                    benefitattachment__benefit=benefit,
                    is_deleted=False
                ).first()
                if bill:
                    cls._create_bill_payment_for_paid_bill(benefit, bill, user)
            except Exception:
                logger.exception("Gateway confirmed the payment but the reconciliation was not saved: "
                                 "benefit %s (code %s)", benefit.id, benefit.code)

    @classmethod
    def _create_bill_payment_for_paid_bill(cls, benefit, bill, user):
        from core import datetime
        from django.contrib.contenttypes.models import ContentType
        from invoice.models import Bill, DetailPaymentInvoice, PaymentInvoice
        from invoice.services import PaymentInvoiceService
        current_date = datetime.date.today()
        bill.status = Bill.Status.RECONCILIATED
        bill.date_payed = current_date
        bill.save(username=user.login_name)
        # Create a BillPayment object for the 'Paid' bill
        bill_payment = {
            "code_tp": bill.code_tp,
            "code_ext": bill.code_ext,
            "code_receipt": bill.code,
            "label": bill.terms,
            'reconciliation_status': PaymentInvoice.ReconciliationStatus.RECONCILIATED,
            "fees": 0.0,
            "amount_received": bill.amount_total,
            "date_payment": current_date,
            'payment_origin': "online payment",
            'payer_ref': 'payment reference',
            'payer_name': 'payer name',
            "json_ext": {}
        }

        bill_payment_details = {
            'subject_type': ContentType.objects.get_for_model(bill),
            'subject': bill,
            'status': DetailPaymentInvoice.DetailPaymentStatus.ACCEPTED,
            'fees': 0.0,
            'amount': bill.amount_total,
            'reconcilation_id': benefit.receipt,
            'reconcilation_date': current_date,
        }
        bill_payment_details = DetailPaymentInvoice(**bill_payment_details)
        payment_service = PaymentInvoiceService(user)
        payment_service.create_with_detail(bill_payment, bill_payment_details)

    @classmethod
    def _get_payroll_bills_amount(cls, payroll):
        from payroll.models import Payroll
        payroll_with_benefit_sum = Payroll.objects.filter(id=payroll.id).annotate(
            total_benefit_amount=Sum('payrollbenefitconsumption__benefit__amount')
        ).first()
        return payroll_with_benefit_sum.total_benefit_amount

    @classmethod
    def _get_benefits_to_string(cls, benefits):
        benefits_uuids = [str(benefit.id) for benefit in benefits]
        benefits_uuids_string = ",".join(benefits_uuids)
        return benefits_uuids_string

    @classmethod
    def _send_payment_data_to_gateway(cls, payroll, user):
        from payroll.models import BenefitConsumptionStatus
        benefits = cls.get_benefits_attached_to_payroll(payroll, BenefitConsumptionStatus.ACCEPTED)
        payment_gateway_connector = cls.PAYMENT_GATEWAY
        benefits_to_approve = []
        for benefit in benefits:
            if send_accepted(payment_gateway_connector.send_payment(benefit.code, benefit.amount)):
                benefits_to_approve.append(benefit)
            else:
                # Handle the case where a benefit payment is rejected
                logger.info(f"Payment for benefit ({benefit.code}) was rejected.")
        failures = []
        if benefits_to_approve:
            failures = cls.approve_for_payment_benefit_consumption(benefits_to_approve, user)
        return {'payroll_id': str(payroll.id), 'succeeded': len(benefits_to_approve),
                'persist_failed': failures}

    @classmethod
    def _process_accepted_payroll(cls, payroll, user, **kwargs):
        from payroll.models import PayrollStatus
        cls.change_status_of_payroll(payroll, PayrollStatus.APPROVE_FOR_PAYMENT, user)

    @classmethod
    def _save_payroll_data(cls, payroll, user, response_from_gateway):
        json_ext = payroll.json_ext if payroll.json_ext else {}
        json_ext['response_from_gateway'] = response_from_gateway
        payroll.json_ext = json_ext
        payroll.save(username=user.username)
        cls._create_payroll_reconcilation_task(payroll, user)

    @classmethod
    @register_service_signal('online_payments.create_task')
    def _create_payroll_reconcilation_task(cls, payroll, user):
        from payroll.apps import PayrollConfig
        from tasks_management.services import TaskService
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        TaskService(user).create({
            'source': 'payroll_reconciliation',
            'entity': payroll,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reconciliation_event,
        })
