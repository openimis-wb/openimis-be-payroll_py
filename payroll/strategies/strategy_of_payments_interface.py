import abc
import logging

logger = logging.getLogger(__name__)

# json_ext key of a benefit a rejection left in place because it may be paid.
REJECTION_HOLD_KEY = 'rejection_hold'


class StrategyOfPaymentInterface(object, metaclass=abc.ABCMeta):

    @classmethod
    def initialize_payment_gateway(cls, payment_point=None):
        pass

    @classmethod
    def accept_payroll(cls, payroll, user, **kwargs):
        pass

    @classmethod
    def make_payment_for_payroll(cls, payroll, user, **kwargs):
        pass

    @classmethod
    def reject_payroll(cls, payroll, user, **kwargs):
        from payroll.models import PayrollStatus
        cls.change_status_of_payroll(payroll, PayrollStatus.REJECTED, user)
        cls.remove_benefits_from_rejected_payroll(payroll)

    @classmethod
    def reject_approved_payroll(cls, payroll, user, **kwargs):
        """Send an approved payroll back to approval without taking back a payment.

        Only a live APPROVE_FOR_PAYMENT payroll is rejected; any other is left
        as it is and the refusal is logged. A benefit the agency has or may
        have paid (APPROVE_FOR_PAYMENT, RECONCILED, or holding a receipt) keeps
        its status, its receipt and its bill payment; ``json_ext.rejection_hold``
        records the rejection on it. The payroll becomes PENDING_APPROVAL and a
        new approval task is created.
        """
        from django.db.models import Q
        from django.utils import timezone
        from core.services.utils.serviceUtils import model_representation
        from payroll.models import (
            BenefitConsumption,
            BenefitConsumptionStatus,
            Payroll,
            PayrollStatus
        )
        from payroll.services import PayrollService

        payroll = Payroll.objects.get(id=payroll.id)
        if payroll.is_deleted or payroll.status != PayrollStatus.APPROVE_FOR_PAYMENT:
            logger.error(
                "Rejection of approved payroll %s refused: status %s%s; only a live %s payroll is rejected.",
                payroll.id, payroll.status, ", deleted" if payroll.is_deleted else "",
                PayrollStatus.APPROVE_FOR_PAYMENT,
            )
            return

        sent_statuses = (BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, BenefitConsumptionStatus.RECONCILED)
        sent = BenefitConsumption.objects.filter(
            Q(status__in=sent_statuses) | (Q(receipt__isnull=False) & ~Q(receipt='')),
            payrollbenefitconsumption__payroll=payroll,
            payrollbenefitconsumption__is_deleted=False,
            is_deleted=False,
        ).distinct()
        rejection = {
            'stage': 'approved_payroll_rejected',
            'at': timezone.now().isoformat(),
            'by': user.login_name,
            'task_id': str(kwargs['task_id']) if kwargs.get('task_id') else None,
            'payroll_id': str(payroll.id),
        }
        for benefit in sent:
            json_ext = dict(benefit.json_ext) if isinstance(benefit.json_ext, dict) else {}
            reason = f'status {benefit.status}' if benefit.status in sent_statuses else 'receipt'
            json_ext[REJECTION_HOLD_KEY] = {**rejection, 'reason': reason}
            benefit.json_ext = json_ext
            benefit.save(username=user.username)
        cls.change_status_of_payroll(payroll, PayrollStatus.PENDING_APPROVAL, user)
        PayrollService(user).create_accept_payroll_task(payroll.id, model_representation(payroll))

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        pass

    @classmethod
    def reconcile_payroll(cls, payroll, user):
        pass

    @classmethod
    def change_status_of_payroll(cls, payroll, status, user):
        payroll.status = status
        payroll.save(username=user.login_name)

    @classmethod
    def remove_benefits_from_rejected_payroll(cls, payroll):
        from payroll.models import (
            BenefitAttachment,
            BenefitConsumption,
            PayrollBenefitConsumption,
        )
        from invoice.models import (
            Bill,
            BillItem
        )

        benefit_data = BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll,
            is_deleted=False
        ).values_list('id', 'benefitattachment__bill')

        if len(benefit_data) > 0:
            benefits, related_bills = zip(*benefit_data)

            BenefitAttachment.objects.filter(
                benefit_id__in=benefits
            ).delete()

            BillItem.objects.filter(
                bill__id__in=related_bills
            ).delete()

            Bill.objects.filter(
                id__in=related_bills
            ).delete()

            PayrollBenefitConsumption.objects.filter(payroll=payroll).delete()

            BenefitConsumption.objects.filter(
                id__in=benefits,
                is_deleted=False
            ).delete()

    @classmethod
    def remove_benefit_from_payroll(cls, benefit):
        from payroll.models import (
            BenefitAttachment,
            BenefitConsumption,
            PayrollBenefitConsumption
        )
        from invoice.models import (
            Bill,
            BillItem
        )

        benefit_data = BenefitConsumption.objects.filter(
            id=benefit.id,
            is_deleted=False
        ).values_list('id', 'benefitattachment__bill')

        if len(benefit_data) > 0:
            benefits, related_bills = zip(*benefit_data)

            BenefitAttachment.objects.filter(
                benefit_id__in=benefits
            ).delete()

            BillItem.objects.filter(
                bill__id__in=related_bills
            ).delete()

            Bill.objects.filter(
                id__in=related_bills
            ).delete()

            PayrollBenefitConsumption.objects.filter(benefit=benefit).delete()

            BenefitConsumption.objects.filter(
                id__in=benefits,
                is_deleted=False
            ).delete()
