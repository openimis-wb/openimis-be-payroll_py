import json
import logging
import pandas as pd
from io import BytesIO

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from simple_history.utils import bulk_create_with_history
from django.db.models import Q
from django.utils.translation import gettext as _

from core import datetime
from core.custom_filters import CustomFilterWizardStorage
from core.utils import to_json_safe_value
from core.models import InteractiveUser
from core.services import BaseService
from core.signals import register_service_signal
from core.services.utils import (
    check_authentication,
    output_exception,
    model_representation,
)
from invoice.models import Bill, BillItem, PaymentInvoice, DetailPaymentInvoice
from invoice.services import PaymentInvoiceService
from payment_cycle.models import PaymentCycle
from payroll.apps import PayrollConfig
from payroll.models import (
    PaymentPoint,
    Payroll,
    PayrollStatus,
    PayrollBenefitConsumption,
    BenefitConsumption,
    BenefitAttachment,
    BenefitConsumptionStatus
)
from payroll.tasks import send_requests_to_gateway_payment, create_payroll_benefits_task
from payroll.validation import PaymentPointValidation, PayrollValidation, BenefitConsumptionValidation
from calculation.services import get_calculation_object
from contribution_plan.models import PaymentPlan
from social_protection.models import Beneficiary, BeneficiaryStatus
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService, _get_std_task_data_payload

logger = logging.getLogger(__name__)


class PaymentPointService(BaseService):
    OBJECT_TYPE = PaymentPoint

    def __init__(self, user, validation_class=PaymentPointValidation):
        super().__init__(user, validation_class)

    @register_service_signal('payment_point_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('payment_point_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('payment_point_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)


PAYROLL_BULK_CREATE_BATCH_SIZE = 500


class PayrollService(BaseService):
    OBJECT_TYPE = Payroll

    def __init__(self, user, validation_class=PayrollValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal('payroll_service.create')
    def create(self, obj_data):
        try:
            self._check_triggers_synced()
            obj_data = self._adjust_create_payload(obj_data)

            obj_data['status'] = PayrollStatus.GENERATING

            json_safe_obj_data = self._recursively_to_json_safe(obj_data)
            obj_data['json_ext'] = {
                **self._get_json_ext_as_dict(obj_data),
                'creation_params': json_safe_obj_data
            }

            # Commit independently so the row persists even if benefit generation fails.
            with transaction.atomic():
                payroll, dict_representation = self._save_payroll(obj_data)

            self._enqueue_benefit_generation(payroll, json_safe_obj_data)

            return dict_representation
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    @register_service_signal('payroll_service.update')
    def update(self, obj_data):
        raise NotImplementedError()

    @check_authentication
    @register_service_signal('payroll_service.delete')
    def delete(self, obj_data):
        payroll_to_delete = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_delete.id}
        TaskService(self.user).create({
            'source': 'payroll_delete',
            'entity': payroll_to_delete,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_delete_event,
            'data': _get_std_task_data_payload(data)
        })

    @check_authentication
    @register_service_signal('payroll_service.retrigger_creation')
    def retrigger_creation(self, obj_data):
        try:
            self._check_triggers_synced()
            payroll = Payroll.objects.get(id=obj_data['id'])
            if payroll.status != PayrollStatus.FAILED:
                raise ValueError(
                    f"{_('payroll.retrigger.invalid_status')}: {payroll.status}"
                )
            creation_params = (payroll.json_ext or {}).get('creation_params')
            if not creation_params:
                raise ValueError(_("payroll.retrigger.creation_params_not_found"))

            # Clean up partial data from the failed attempt before retrying
            self._cleanup_payroll_benefits(payroll)

            payroll.status = PayrollStatus.GENERATING
            if payroll.json_ext:
                payroll.json_ext.pop('creation_error', None)
            payroll.save(username=self.user.login_name)

            self._enqueue_benefit_generation(payroll, creation_params)
            return model_representation(payroll)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="retrigger_creation", exception=exc)

    @check_authentication
    @register_service_signal('payroll_service.attach_benefit_to_payroll')
    def attach_benefit_to_payroll(self, payroll_id, benefit_id):
        payroll_benefit = PayrollBenefitConsumption(payroll_id=payroll_id, benefit_id=benefit_id)
        payroll_benefit.save(user=self.user)

    def bulk_attach_benefits(self, payroll_benefit_consumptions):
        return bulk_create_with_history(
            payroll_benefit_consumptions, PayrollBenefitConsumption,
            batch_size=PAYROLL_BULK_CREATE_BATCH_SIZE, default_user=self.user,
        )

    @register_service_signal('payroll_service.create_task')
    def create_accept_payroll_task(self, payroll_id, obj_data):
        payroll_to_accept = Payroll.objects.get(id=payroll_id)
        data = {**obj_data, 'id': payroll_id}
        TaskService(self.user).create({
            'source': 'payroll',
            'entity': payroll_to_accept,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_accept_event,
            'data': _get_std_task_data_payload(data)
        })

    @register_service_signal('payroll_service.close_payroll')
    def close_payroll(self, obj_data):
        payroll_to_close = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_close.id}
        TaskService(self.user).create({
            'source': 'payroll_reconciliation',
            'entity': payroll_to_close,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reconciliation_event,
            'data': _get_std_task_data_payload(data)
        })

    @register_service_signal('payroll_service.reject_approve_payroll')
    def reject_approved_payroll(self, obj_data):
        payroll_to_reject = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_reject.id}
        TaskService(self.user).create({
            'source': 'payroll_reject',
            'entity': payroll_to_reject,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reject_event,
            'data': _get_std_task_data_payload(data)
        })

    def make_payment_for_payroll(self, obj_data):
        payroll_id = obj_data['id']
        send_requests_to_gateway_payment.delay(str(payroll_id), str(self.user.id))

    def _enqueue_benefit_generation(self, payroll, obj_data):
        """Dispatch Celery task for benefit generation. Marks payroll FAILED on enqueue failure."""
        try:
            create_payroll_benefits_task.delay(
                str(payroll.id), str(self.user.id), dict(obj_data)
            )
        except Exception as task_exc:
            logger.error(f"Failed to enqueue benefit generation for payroll {payroll.id}: {task_exc}", exc_info=True)
            payroll.status = PayrollStatus.FAILED
            payroll.json_ext = {**(payroll.json_ext or {}), 'creation_error': str(task_exc)}
            payroll.save(username=self.user.login_name)
            raise

    def _check_triggers_synced(self):
        from invoice.apps import InvoiceConfig
        from payroll.apps import PayrollConfig
        if not InvoiceConfig.bill_trigger_synced or not PayrollConfig.benefit_trigger_synced:
            if self.user and hasattr(self.user, 'is_superuser') and self.user.is_superuser:
                raise ValueError(_("payroll.create.triggers_not_synced.admin"))
            raise ValueError(_("payroll.create.triggers_not_synced"))

    def _recursively_to_json_safe(self, obj):
        if isinstance(obj, dict):
            return {k: self._recursively_to_json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [self._recursively_to_json_safe(v) for v in obj]
        return to_json_safe_value(obj)

    @transaction.atomic
    def _cleanup_payroll_benefits(self, payroll):
        """Remove all benefits/bills from a failed payroll before retrying."""
        pbc_qs = PayrollBenefitConsumption.objects.filter(payroll=payroll)
        benefit_ids = list(pbc_qs.values_list('benefit_id', flat=True))
        if not benefit_ids:
            return
        bill_ids = list(
            BenefitAttachment.objects.filter(
                benefit_id__in=benefit_ids
            ).values_list('bill_id', flat=True)
        )
        BenefitAttachment.objects.filter(benefit_id__in=benefit_ids).delete()
        pbc_qs.delete()
        BenefitConsumption.objects.filter(id__in=benefit_ids).delete()
        if bill_ids:
            BillItem.objects.filter(bill_id__in=bill_ids).delete()
            Bill.objects.filter(id__in=bill_ids).delete()
        logger.info(
            f"Cleaned up {len(benefit_ids)} benefits and {len(bill_ids)} bills "
            f"from failed payroll {payroll.id}"
        )

    def _save_payroll(self, obj_data):
        obj_ = self.OBJECT_TYPE(**obj_data)
        dict_representation = self.save_instance(obj_)
        payroll_id = dict_representation["data"]["id"]
        payroll = Payroll.objects.get(id=payroll_id)
        return payroll, dict_representation

    def _get_json_ext_as_dict(self, obj_data):
        json_ext = obj_data.get("json_ext") or {}
        if isinstance(json_ext, str):
            try:
                json_ext = json.loads(json_ext)
            except (ValueError, TypeError):
                logger.warning(f"Could not parse json_ext as JSON; treating as empty: {json_ext!r}", exc_info=True)
                json_ext = {}
        return json_ext

    def _get_payment_plan(self, obj_data):
        payment_plan_id = obj_data.get("payment_plan_id")
        payment_plan = PaymentPlan.objects.get(id=payment_plan_id)
        return payment_plan

    def _get_payment_cycle(self, obj_data):
        payment_cycle_id = obj_data.get("payment_cycle_id")
        payment_cycle = PaymentCycle.objects.get(id=payment_cycle_id)
        return payment_cycle

    def _get_dates_parameter(self, obj_data):
        date_valid_from = obj_data.get('date_valid_from', None)
        date_valid_to = obj_data.get('date_valid_to', None)
        return date_valid_from, date_valid_to

    def _select_beneficiary_based_on_criteria(self, obj_data, payment_plan):
        json_ext = self._get_json_ext_as_dict(obj_data)

        beneficiaries_queryset = Beneficiary.objects.filter(
            benefit_plan__id=payment_plan.benefit_plan.id,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        )

        filter_criteria = json_ext.get("filter_criteria", {})

        project_ids = filter_criteria.get("project_ids", [])
        if project_ids:
            beneficiaries_queryset = beneficiaries_queryset.filter(
                project__id__in=project_ids
            )

        location_ids = filter_criteria.get("location_ids", [])
        if location_ids:
            beneficiaries_queryset = beneficiaries_queryset.filter(
                Q(individual__location__uuid__in=location_ids)
                | Q(individual__location__parent__uuid__in=location_ids)
                | Q(individual__location__parent__parent__uuid__in=location_ids)
                | Q(individual__location__parent__parent__parent__uuid__in=location_ids)
            )

        custom_filters = [
            criterion["custom_filter_condition"]
            for criterion in json_ext.get("advanced_criteria", [])
        ]
        if custom_filters:
            beneficiaries_queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                PayrollConfig.name,
                "BenefitPlan",
                custom_filters,
                beneficiaries_queryset,
            )

        return beneficiaries_queryset

    def _generate_benefits(self, payment_plan, beneficiaries_queryset, date_from, date_to, payroll, payment_cycle):
        calculation = get_calculation_object(payment_plan.calculation)
        calculation.calculate_if_active_for_object(
            payment_plan,
            user_id=self.user.id,
            start_date=date_from, end_date=date_to,
            beneficiaries_queryset=beneficiaries_queryset,
            payroll=payroll,
            payment_cycle=payment_cycle
        )

    @transaction.atomic
    def _move_benefit_consumptions(self, payroll, from_payroll_id):
        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll_id=from_payroll_id,
            benefit__status__in=[BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT]
        )
        payroll_benefits.update(payroll=payroll)
        benefits = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll)
        benefits.update(status=BenefitConsumptionStatus.ACCEPTED)

    _OPENSEARCH_SYNC_LOCK_ID = 0x4F53_5059

    def _create_payroll_benefits(self, payroll, obj_data):
        obj_data = dict(obj_data)  # shallow copy — don't mutate caller's dict

        try:
            from opensearch_reports.models import OpenSearchDashboard
        except ImportError:
            OpenSearchDashboard = None

        dashboards_to_toggle = ['Payment', 'Invoice']

        try:
            try:
                if OpenSearchDashboard:
                    self._disable_opensearch_sync(dashboards_to_toggle)
                from_failed_invoices_payroll_id = obj_data.pop("from_failed_invoices_payroll_id", None)
                payment_plan = self._get_payment_plan(obj_data)
                payment_cycle = self._get_payment_cycle(obj_data)
                date_valid_from, date_valid_to = self._get_dates_parameter(obj_data)

                if not bool(from_failed_invoices_payroll_id):
                    beneficiaries_queryset = self._select_beneficiary_based_on_criteria(obj_data, payment_plan)
                    self._generate_benefits(
                        payment_plan,
                        beneficiaries_queryset,
                        date_valid_from,
                        date_valid_to,
                        payroll,
                        payment_cycle
                    )
                else:
                    self._move_benefit_consumptions(payroll, from_failed_invoices_payroll_id)

                if payroll.status != PayrollStatus.PENDING_APPROVAL:
                    payroll.status = PayrollStatus.PENDING_APPROVAL
                    payroll.save(username=self.user.login_name)
                self.create_accept_payroll_task(payroll.id, obj_data)

                if OpenSearchDashboard:
                    self._trigger_opensearch_reindex(payroll)
            finally:
                if OpenSearchDashboard:
                    self._reenable_opensearch_sync(dashboards_to_toggle, payroll.id)

        except Exception as exc:
            logger.error(f"Error in _create_payroll_benefits for payroll {payroll.id}: {exc}", exc_info=True)
            try:
                payroll.status = PayrollStatus.FAILED
                if payroll.json_ext is None:
                    payroll.json_ext = {}
                payroll.json_ext['creation_error'] = str(exc)
                payroll.json_ext.pop('progress', None)
                payroll.save(username=self.user.login_name)
            except Exception as e:
                logger.error(f"Failed to update payroll {payroll.id} status to FAILED: {e}", exc_info=True)
            raise

    @staticmethod
    def _disable_opensearch_sync(dashboards_to_toggle):
        from opensearch_reports.models import OpenSearchDashboard
        from django.db import connection
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [PayrollService._OPENSEARCH_SYNC_LOCK_ID])
        OpenSearchDashboard.objects.filter(name__in=dashboards_to_toggle).update(synch_disabled=True)

    @staticmethod
    def _reenable_opensearch_sync(dashboards_to_toggle, payroll_id):
        from opensearch_reports.models import OpenSearchDashboard
        from django.db import connection
        try:
            other_generating = Payroll.objects.filter(
                status=PayrollStatus.GENERATING
            ).exclude(id=payroll_id).exists()
            if not other_generating:
                OpenSearchDashboard.objects.filter(
                    name__in=dashboards_to_toggle
                ).update(synch_disabled=False)
        except Exception as e:
            logger.error(
                f"Failed to re-enable OpenSearch sync for payroll {payroll_id}: {e}",
                exc_info=True,
            )
        finally:
            if connection.vendor == 'postgresql':
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", [PayrollService._OPENSEARCH_SYNC_LOCK_ID])
                except Exception as e:
                    logger.error(f"Failed to release advisory lock: {e}", exc_info=True)

    def _trigger_opensearch_reindex(self, payroll):
        """Trigger OpenSearch indexing for payroll-related entities."""
        try:
            from django_opensearch_dsl.registries import registry
        except ImportError:
            return

        try:
            registry.update(payroll)

            benefits = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll)
            registry.update(benefits)

            pbcs = PayrollBenefitConsumption.objects.filter(payroll=payroll)
            registry.update(pbcs)

            bills = Bill.objects.filter(benefitattachment__benefit__payrollbenefitconsumption__payroll=payroll).distinct()
            registry.update(bills)
            registry.update(BillItem.objects.filter(bill__in=bills))

            attachments = BenefitAttachment.objects.filter(benefit__payrollbenefitconsumption__payroll=payroll)
            registry.update(attachments)

        except Exception as e:
            logger.error(f"Failed to trigger OpenSearch re-indexing for payroll {payroll.id}: {e}", exc_info=True)


class BenefitConsumptionService(BaseService):
    OBJECT_TYPE = BenefitConsumption

    def __init__(self, user, validation_class=BenefitConsumptionValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal('benefit_consumption_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('benefit_consumption_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @check_authentication
    @register_service_signal('benefit_consumption_service.delete')
    def delete(self, obj_data):
        benefit_to_delete = BenefitConsumption.objects.get(id=obj_data['id'])
        benefit_to_delete.status = BenefitConsumptionStatus.PENDING_DELETION
        benefit_to_delete.save(user=self.user)
        data = {'id': benefit_to_delete.id}
        TaskService(self.user).create({
            'source': 'benefit_delete',
            'entity': benefit_to_delete,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.benefit_delete_event,
            'data': _get_std_task_data_payload(data)
        })

    @check_authentication
    @register_service_signal('benefit_consumption_service.create_or_update_benefit_attachment')
    def create_or_update_benefit_attachment(self, bills_queryset, benefit_id):
        # remove first old attachments and save the new one
        BenefitAttachment.objects.filter(benefit_id=benefit_id).delete()
        # save new bill attachments
        for bill in bills_queryset:
            benefit_attachment = BenefitAttachment(bill_id=bill.id, benefit_id=benefit_id)
            benefit_attachment.save(user=self.user)

    def bulk_create(self, benefits):
        """Bulk-create BenefitConsumptions with history. Returns instances with DB-assigned codes."""
        from invoice.trigger_sync import refresh_trigger_codes
        created = bulk_create_with_history(
            benefits, BenefitConsumption,
            batch_size=PAYROLL_BULK_CREATE_BATCH_SIZE, default_user=self.user,
        )
        return refresh_trigger_codes(created, BenefitConsumption, batch_size=PAYROLL_BULK_CREATE_BATCH_SIZE)

    def bulk_create_attachments(self, attachments):
        return bulk_create_with_history(
            attachments, BenefitAttachment,
            batch_size=PAYROLL_BULK_CREATE_BATCH_SIZE, default_user=self.user,
        )


class CsvReconciliationService:
    def __init__(self, user: InteractiveUser):
        self.user = user

    def download_reconciliation(self, payroll_id) -> BytesIO:
        payroll = self._resolve_payroll(payroll_id)
        bc_qs = self._get_benefit_consumption_qs(payroll)
        # Retrieve the basic fields
        field_keys = list(PayrollConfig.csv_reconciliation_field_mapping.keys())
        records = list(bc_qs.values(*field_keys))

        # Collect all extra_info keys to ensure all columns are present in the DataFrame
        extra_info_keys = set()
        extra_info_dicts = []  # To store extra_info dicts for each record
        for record in records:
            bc = bc_qs.get(code=record['code'])
            extra_info = bc.json_ext.get('extra_info', {}) if bc.json_ext else {}
            extra_info_keys.update(extra_info.keys())
            extra_info_dicts.append(extra_info)

        # Convert to DataFrame
        df = pd.DataFrame.from_records(records)

        for key in extra_info_keys:
            if key not in df.columns:
                df[key] = None

        # Add paid extra field
        df[PayrollConfig.csv_reconciliation_paid_extra_field] = df.apply(
            lambda row: self._fill_paid_column(row), axis=1
        )
        df.rename(columns=PayrollConfig.csv_reconciliation_field_mapping, inplace=True)

        # Add extra_info fields at the end of the DataFrame
        for key in extra_info_keys:
            df[key] = [extra_info_dict.get(key, None) for extra_info_dict in extra_info_dicts]

        in_memory_file = BytesIO()
        # BytesIO is duck-typed as a file object, so it can be passed to df.to_csv
        # noinspection PyTypeChecker
        df.to_csv(in_memory_file, index=False)
        return in_memory_file

    def upload_reconciliation(self, payroll_id, file, upload):
        payroll = self._resolve_payroll(payroll_id)
        upload.payroll = payroll
        upload.status = upload.Status.IN_PROGRESS
        upload.save(username=self.user.login_name)
        if not file:
            raise ValueError(_('csv_reconciliation.validation.file_required'))
        df = pd.read_csv(file)
        self._validate_dataframe(df)
        df.rename(columns={v: k for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()}, inplace=True)

        affected_rows = 0
        skipped_items = 0
        total_number_of_benefits_in_file = len(df)

        df[PayrollConfig.csv_reconciliation_errors_column] = df.apply(lambda row: self._reconcile_row(payroll, row),
                                                                      axis=1)

        for __, row in df.iterrows():
            if not pd.isna(row[PayrollConfig.csv_reconciliation_errors_column]):
                skipped_items += 1
            else:
                affected_rows += 1

        summary = {
            'affected_rows': affected_rows,
            'total_number_of_benefits_in_file': total_number_of_benefits_in_file,
            'skipped_items': skipped_items
        }

        error_df = df[df[PayrollConfig.csv_reconciliation_errors_column].apply(lambda x: bool(x))]
        if not error_df.empty:
            in_memory_file = BytesIO()
            df.rename(columns={k: v for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()}, inplace=True)
            df.to_csv(in_memory_file, index=False)
            return in_memory_file, error_df.set_index(PayrollConfig.csv_reconciliation_code_column)[
                PayrollConfig.csv_reconciliation_errors_column
            ].to_dict(), summary
        return file, None, summary

    def _get_benefit_consumption_qs(self, payroll):
        qs = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll, is_deleted=False)
        if not qs.exists():
            raise ValueError('csv_reconciliation.validation.no_benefit_consumption_for_payroll')
        return qs

    def _validate_dataframe(self, df):
        if df is None:
            raise ValueError(_("Unknown error while loading import file"))
        if df.empty:
            raise ValueError(_("Import file is empty"))
        if PayrollConfig.csv_reconciliation_errors_column in df.columns:
            raise ValueError(_("Column errors in csv."))
        if 'Status' in df.columns:
            if (df[PayrollConfig.csv_reconciliation_status_column] == BenefitConsumptionStatus.RECONCILED).all():
                raise ValueError(_("All of the Benefit Consumptions have been already reconciled."))

    def _fill_paid_column(self, row):
        if (PayrollConfig.csv_reconciliation_status_column in row
                and row[PayrollConfig.csv_reconciliation_status_column] == BenefitConsumptionStatus.RECONCILED):
            return PayrollConfig.csv_reconciliation_paid_yes
        else:
            return None

    def _resolve_payroll(self, payroll_id):
        if not payroll_id:
            raise ValueError('csv_reconciliation.validation.payroll_id_required')
        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if not payroll:
            raise ValueError('csv_reconciliation.validation.payroll_not_found')
        return payroll

    def _reconcile_row(self, payroll, row):
        errors = []
        bc = BenefitConsumption.objects.filter(code=row['code'], is_deleted=False).first()
        if not bc:
            errors.append(_('benefit_consumption_not_found'))
            return errors
        if not bc.payrollbenefitconsumption_set.filter(payroll=payroll).exists():
            errors.append(_('benefit_consumption_not_in_payroll'))
        if (row[PayrollConfig.csv_reconciliation_paid_extra_field]
                and row[PayrollConfig.csv_reconciliation_paid_extra_field]
                not in [PayrollConfig.csv_reconciliation_paid_yes, PayrollConfig.csv_reconciliation_paid_no]):
            errors.append(_('paid_column_invalid_value'))

        if not row[PayrollConfig.csv_reconciliation_receipt_column]:
            errors.append(_('receipt_required'))

        if bc and bc.status != row['status']:
            errors.append(_('status_not_matching'))

        if (not errors
                and (row[PayrollConfig.csv_reconciliation_paid_extra_field] == PayrollConfig.csv_reconciliation_paid_yes
                     and bc.status == BenefitConsumptionStatus.ACCEPTED)):
            self._reconcile_bc(row, bc)

        return errors if errors else None

    def _reconcile_bc(self, row, bc):
        bc.status = BenefitConsumptionStatus.RECONCILED
        bc.receipt = row[PayrollConfig.csv_reconciliation_receipt_column]
        extra_info = {k: row[k] for k in row.index
                      if k not in PayrollConfig.csv_reconciliation_field_mapping and not pd.isna(row[k])}
        bc.json_ext = {'extra_info': extra_info}
        bc.save(username=self.user.login_name)
        bill = Bill.objects.filter(benefitattachment__benefit=bc, is_deleted=False).first()
        if bill:
            self._reconcile_bill(row, bill)

    def _reconcile_bill(self, row, bill):
        current_date = datetime.date.today()
        bill.status = Bill.Status.RECONCILIATED
        bill.date_payed = current_date
        bill.save(username=self.user.login_name)

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
            'reconcilation_id': row[PayrollConfig.csv_reconciliation_receipt_column],
            'reconcilation_date': current_date,
        }
        bill_payment_details = DetailPaymentInvoice(**bill_payment_details)
        payment_service = PaymentInvoiceService(self.user)
        payment_service.create_with_detail(bill_payment, bill_payment_details)
