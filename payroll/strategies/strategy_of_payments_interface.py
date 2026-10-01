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
        """Reject a payroll at approval: it becomes REJECTED and its rows are
        released as ``remove_benefits_from_rejected_payroll`` does."""
        from payroll.models import PayrollStatus
        cls.change_status_of_payroll(payroll, PayrollStatus.REJECTED, user)
        cls.remove_benefits_from_rejected_payroll(
            payroll, user=user, stage='payroll_rejected', task_id=kwargs.get('task_id'))

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
    def sent_benefit_reasons(cls, benefits):
        """{benefit id: reason} for the benefits an agency may have paid:
        status APPROVE_FOR_PAYMENT or RECONCILED, or a receipt."""
        from payroll.models import BenefitConsumptionStatus
        sent_statuses = (BenefitConsumptionStatus.APPROVE_FOR_PAYMENT, BenefitConsumptionStatus.RECONCILED)
        reasons = {}
        for benefit in benefits:
            if benefit.status in sent_statuses:
                reasons[benefit.id] = f'status {benefit.status}'
            elif benefit.receipt:
                reasons[benefit.id] = 'receipt'
        return reasons

    @classmethod
    def delete_payroll(cls, payroll, user, **kwargs):
        """Delete a payroll whose deletion task was approved; True when deleted.

        A payroll holding a benefit an agency may have paid
        (``sent_benefit_reasons``) is not deleted: the refusal is logged and
        nothing changes. Otherwise its benefits are removed
        (``remove_benefits_from_rejected_payroll``) and the payroll is
        deleted.
        """
        from payroll.models import BenefitConsumption, Payroll
        from payroll.services import PayrollService

        payroll = Payroll.objects.get(id=payroll.id)
        benefits = list(BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll,
            payrollbenefitconsumption__is_deleted=False,
            is_deleted=False,
        ).distinct())
        sent = cls.sent_benefit_reasons(benefits)
        if sent:
            logger.error(
                "Deletion of payroll %s refused: %d benefit(s) may have been paid (%s); "
                "nothing was deleted.",
                payroll.id, len(sent), ', '.join(sorted(set(sent.values()))),
            )
            return False
        cls.remove_benefits_from_rejected_payroll(
            payroll, user=user, stage='payroll_deleted', task_id=kwargs.get('task_id'))
        PayrollService(user).delete_instance(payroll)
        return True

    @classmethod
    def remove_benefits_from_rejected_payroll(cls, payroll, user=None, **kwargs):
        """Remove the benefits of a rejected or deleted payroll that no agency
        may have paid.

        A benefit an agency may have paid (``sent_benefit_reasons``) keeps its
        row, link, status, receipt and bill; ``json_ext.rejection_hold``
        records why, with ``kwargs['stage']`` and ``kwargs['task_id']``. Every
        other benefit is deleted with its attachments, bill items, bills and
        its links to this payroll. Returns the held reasons by benefit id.
        """
        from django.utils import timezone
        from payroll.models import (
            BenefitAttachment,
            BenefitConsumption,
            PayrollBenefitConsumption,
        )
        from invoice.models import (
            Bill,
            BillItem
        )

        benefits = list(BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll,
            is_deleted=False,
        ).distinct())
        held = cls.sent_benefit_reasons(benefits)
        hold = {
            'stage': kwargs.get('stage') or 'payroll_rejected',
            'at': timezone.now().isoformat(),
            'by': user.login_name if user else None,
            'task_id': str(kwargs['task_id']) if kwargs.get('task_id') else None,
            'payroll_id': str(payroll.id),
        }
        for benefit in benefits:
            if benefit.id in held:
                json_ext = dict(benefit.json_ext) if isinstance(benefit.json_ext, dict) else {}
                json_ext[REJECTION_HOLD_KEY] = {**hold, 'reason': held[benefit.id]}
                benefit.json_ext = json_ext
                benefit.save(user=user)
        if held:
            logger.warning("Payroll %s: %d benefit(s) that may have been paid kept, not deleted",
                           payroll.id, len(held))

        released = [benefit.id for benefit in benefits if benefit.id not in held]
        if not released:
            return held
        related_bills = list(BenefitAttachment.objects.filter(
            benefit_id__in=released).values_list('bill_id', flat=True))
        BenefitAttachment.objects.filter(benefit_id__in=released).delete()
        BillItem.objects.filter(bill__id__in=related_bills).delete()
        Bill.objects.filter(id__in=related_bills).delete()
        PayrollBenefitConsumption.objects.filter(payroll=payroll, benefit_id__in=released).delete()
        BenefitConsumption.objects.filter(id__in=released, is_deleted=False).delete()
        return held

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
