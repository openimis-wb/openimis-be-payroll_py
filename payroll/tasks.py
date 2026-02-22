import logging
from celery import shared_task

from core.models import User
from payroll.models import (
    Payroll,
    PayrollStatus,
    BenefitConsumptionStatus,
    BenefitConsumption,
    PayrollBenefitConsumption,
    BenefitAttachment
)
from invoice.models import Bill, BillItem
from payroll.strategies import StrategyOnlinePayment
from payroll.payments_registry import PaymentMethodStorage
try:
    from django_opensearch_dsl.registries import registry
except ImportError:
    registry = None

logger = logging.getLogger(__name__)


@shared_task
def send_requests_to_gateway_payment(payroll_id, user_id):
    payroll = Payroll.objects.get(id=payroll_id)
    strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
    if strategy:
        user = User.objects.get(id=user_id)
        strategy.initialize_payment_gateway(payroll.payment_point)
        strategy.make_payment_for_payroll(payroll, user)


@shared_task
def send_request_to_reconcile(payroll_id, user_id):
    payroll = Payroll.objects.get(id=payroll_id)
    user = User.objects.get(id=user_id)
    strategy = StrategyOnlinePayment
    strategy.initialize_payment_gateway(payroll.payment_point)
    strategy.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)
    benefits = strategy.get_benefits_attached_to_payroll(payroll, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)
    payment_gateway_connector = strategy.PAYMENT_GATEWAY
    benefits_to_reconcile = []
    for benefit in benefits:
        is_reconciled = payment_gateway_connector.reconcile(benefit.code, benefit.amount)
        # Initialize json_ext if it is None
        if benefit.json_ext is None:
            benefit.json_ext = {}
        if is_reconciled:
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = True
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefits_to_reconcile.append(benefit)
        else:
            # Handle the case where a benefit payment is rejected
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = False
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefit.save(username=user.login_name)
            logger.info(f"Payment for benefit ({benefit.code}) was rejected.")
    if benefits_to_reconcile:
        strategy.reconcile_benefit_consumption(benefits_to_reconcile, user)


@shared_task
def create_payroll_benefits_task(payroll_id, user_id, obj_data):
    from payroll.services import PayrollService
    try:
        from opensearch_reports.models import OpenSearchDashboard
    except ImportError:
        OpenSearchDashboard = None

    dashboards_to_toggle = ['Payment', 'Invoice']

    try:
        user = User.objects.get(id=user_id)
        payroll = Payroll.objects.get(id=payroll_id)
        payroll_service = PayrollService(user)

        if OpenSearchDashboard:
            OpenSearchDashboard.objects.filter(name__in=dashboards_to_toggle).update(synch_disabled=True)

        try:
            from_failed_invoices_payroll_id = obj_data.pop("from_failed_invoices_payroll_id", None)
            payment_plan = payroll_service._get_payment_plan(obj_data)
            payment_cycle = payroll_service._get_payment_cycle(obj_data)
            date_valid_from, date_valid_to = payroll_service._get_dates_parameter(obj_data)
            
            if not bool(from_failed_invoices_payroll_id):
                beneficiaries_queryset = payroll_service._select_beneficiary_based_on_criteria(obj_data, payment_plan)
                payroll_service._generate_benefits(
                    payment_plan,
                    beneficiaries_queryset,
                    date_valid_from,
                    date_valid_to,
                    payroll,
                    payment_cycle
                )
            else:
                payroll_service._move_benefit_consumptions(payroll, from_failed_invoices_payroll_id)
                
            if payroll.status != PayrollStatus.PENDING_APPROVAL:
                payroll.status = PayrollStatus.PENDING_APPROVAL
                payroll.save(username=user.login_name)
            payroll_service.create_accept_payroll_task(payroll.id, obj_data)
        finally:
            if OpenSearchDashboard:
                OpenSearchDashboard.objects.filter(name__in=dashboards_to_toggle).update(synch_disabled=False)
                _trigger_opensearch_reindex(payroll)
        
    except Exception as exc:
        logger.error(f"Error in create_payroll_benefits_task for payroll {payroll_id}: {exc}", exc_info=exc)
        try:
            payroll = Payroll.objects.get(id=payroll_id)
            payroll.status = PayrollStatus.FAILED
            if payroll.json_ext is None:
                payroll.json_ext = {}
            payroll.json_ext['creation_error'] = str(exc)
            payroll.save()
        except Exception as e:
            logger.error(f"Failed to update payroll status to FAILED: {e}")
        raise


def _trigger_opensearch_reindex(payroll):
    """
    Manually trigger OpenSearch indexing for all entities related to the payroll.
    """
    if not registry:
        return

    try:
        registry.update(payroll)

        benefits = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll)
        if benefits.exists():
            registry.update(benefits)

        pbcs = PayrollBenefitConsumption.objects.filter(payroll=payroll)
        if pbcs.exists():
            registry.update(pbcs)

        bills = Bill.objects.filter(benefitattachment__benefit__payrollbenefitconsumption__payroll=payroll).distinct()
        if bills.exists():
            registry.update(bills)
            bill_items = BillItem.objects.filter(bill__in=bills)
            if bill_items.exists():
                registry.update(bill_items)

        attachments = BenefitAttachment.objects.filter(benefit__payrollbenefitconsumption__payroll=payroll)
        if attachments.exists():
            registry.update(attachments)

    except Exception as e:
        logger.warning(f"Failed to trigger OpenSearch re-indexing for payroll {payroll.id}: {e}")

