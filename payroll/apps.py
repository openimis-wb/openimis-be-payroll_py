import logging
import os

from django.apps import AppConfig

from core.custom_filters import CustomFilterRegistryPoint
from payroll.payments_registry import PaymentsMethodRegistryPoint

logger = logging.getLogger(__name__)

MODULE_NAME = 'payroll'

DEFAULT_CONFIG = {
    "gql_payment_point_search_perms": ["201001"],
    "gql_payment_point_create_perms": ["201002"],
    "gql_payment_point_update_perms": ["201003"],
    "gql_payment_point_delete_perms": ["201004"],
    "gql_payroll_search_perms": ["202001"],
    "gql_payroll_create_perms": ["202002"],
    "gql_payroll_delete_perms": ["202004"],
    "gql_csv_reconciliation_search_perms": ["206001"],
    "gql_csv_reconciliation_create_perms": ["206002"],
    "payroll_accept_event": "payroll.accept_payroll",
    "payroll_reconciliation_event": "payroll.payroll_reconciliation",
    "payroll_reject_event": "payroll.payroll_reject",
    "csv_reconciliation_field_mapping": {
        'payrollbenefitconsumption__payroll__name': 'Payroll Name',
        'payrollbenefitconsumption__payroll__status': 'Payroll Status',
        'individual__first_name': 'First Name',
        'individual__last_name': 'Last Name',
        'individual__dob': 'Date of Birth',
        'code': 'Code',
        'status': 'Status',
        'amount': 'Amount',
        'type': 'Type',
        'receipt': 'Receipt',
    },
    "csv_reconciliation_status_column": "Status",
    "csv_reconciliation_paid_extra_field": "Paid",
    "csv_reconciliation_receipt_column": "receipt",
    "csv_reconciliation_errors_column": "errors",
    "csv_reconciliation_code_column": "code",
    "csv_reconciliation_paid_yes": "Yes",
    "csv_reconciliation_paid_no": "No",
    "payroll_delete_event": "payroll.payroll_delete",
    "benefit_delete_event": "payroll.benefit_delete",

    "gateway_base_url": "http://41.175.18.170:8070/api/mobile/v1/",
    "endpoint_payment": "mock/payment",
    "endpoint_reconciliation": "mock/reconciliation",
    "payment_gateway_api_key": os.getenv('PAYMENT_GATEWAY_API_KEY'),
    "payment_gateway_basic_auth_username": os.getenv('PAYMENT_GATEWAY_BASIC_AUTH_USERNAME'),
    "payment_gateway_basic_auth_password": os.getenv('PAYMENT_GATEWAY_BASIC_AUTH_PASSWORD'),
    "payment_gateway_timeout": 5,
    "payment_gateway_auth_type": "basic",  # can be 'token' or 'basic'
    "payment_gateway_class": "payroll.payment_gateway.MockedPaymentGatewayConnector",
    "receipt_length": 8,
    "benefit_code_pattern": "BEN-[YY]-[SEQ:10]",
    "bulk_create_batch_size": 500,
}


class PayrollConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    gql_payment_point_search_perms = None
    gql_payment_point_create_perms = None
    gql_payment_point_update_perms = None
    gql_payment_point_delete_perms = None
    gql_payroll_search_perms = None
    gql_payroll_create_perms = None
    gql_payroll_delete_perms = None
    gql_csv_reconciliation_search_perms = None
    gql_csv_reconciliation_create_perms = None
    payroll_accept_event = None
    payroll_reconciliation_event = None
    payroll_reject_event = None
    csv_reconciliation_field_mapping = None
    csv_reconciliation_status_column = None
    csv_reconciliation_paid_extra_field = None
    csv_reconciliation_receipt_column = None
    csv_reconciliation_errors_column = None
    csv_reconciliation_code_column = None
    csv_reconciliation_paid_yes = None
    csv_reconciliation_paid_no = None
    payroll_delete_event = None
    benefit_delete_event = None

    gateway_base_url = None
    endpoint_payment = None
    endpoint_reconciliation = None
    payment_gateway_api_key = None
    payment_gateway_basic_auth_username = None
    payment_gateway_basic_auth_password = None
    payment_gateway_timeout = None
    payment_gateway_auth_type = None
    payment_gateway_class = None
    receipt_length = None
    benefit_code_pattern = None
    bulk_create_batch_size = None
    benefit_trigger_synced = False

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)
        self.__register_filters_and_payment_methods()
        self._sync_benefit_trigger()
        self._connect_config_signal()

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(PayrollConfig, field):
                setattr(PayrollConfig, field, cfg[field])

    def __register_filters_and_payment_methods(cls):
        from social_protection.custom_filters import BenefitPlanCustomFilterWizard
        CustomFilterRegistryPoint.register_custom_filters(
            module_name=cls.name,
            custom_filter_class_list=[BenefitPlanCustomFilterWizard]
        )

        from payroll.strategies import (
            StrategyOfflinePayment,
            StrategyOnlinePayment
        )
        PaymentsMethodRegistryPoint.register_payment_method(
            payment_method_class_list=[
                StrategyOfflinePayment(),
                StrategyOnlinePayment(),
            ]
        )

    def _sync_benefit_trigger(self):
        try:
            from payroll.models import BenefitConsumption
            from invoice.trigger_sync import sync_trigger
            sync_trigger(
                model=BenefitConsumption,
                sequence_name='benefit_code_seq',
                trigger_name='benefit_code_trigger',
                code_column='code',
                pattern=self.benefit_code_pattern or DEFAULT_CONFIG['benefit_code_pattern'],
                pg_function_name='set_benefit_code',
            )
            PayrollConfig.benefit_trigger_synced = True
        except Exception as e:
            PayrollConfig.benefit_trigger_synced = False
            logger.error(f"Benefit trigger sync failed: {e}", exc_info=True)

    def _connect_config_signal(self):
        from django.db.models.signals import post_save
        from core.models import ModuleConfiguration
        post_save.connect(
            self._on_config_change, sender=ModuleConfiguration,
            dispatch_uid='payroll.benefit_code_trigger_sync',
        )

    @staticmethod
    def _on_config_change(sender, instance, **kwargs):
        import json
        if instance.module != MODULE_NAME or instance.layer != 'be':
            return
        try:
            cfg = json.loads(instance.config) if isinstance(instance.config, str) else instance.config
            pattern = cfg.get('benefit_code_pattern') or DEFAULT_CONFIG['benefit_code_pattern']
            from payroll.models import BenefitConsumption
            from invoice.trigger_sync import sync_trigger, validate_pattern
            validate_pattern(pattern)
            PayrollConfig.benefit_code_pattern = pattern
            sync_trigger(
                model=BenefitConsumption,
                sequence_name='benefit_code_seq',
                trigger_name='benefit_code_trigger',
                code_column='code',
                pattern=pattern,
                pg_function_name='set_benefit_code',
            )
            PayrollConfig.benefit_trigger_synced = True
            logger.info(f"Benefit trigger updated after config change (pattern: {pattern})")
        except Exception as e:
            PayrollConfig.benefit_trigger_synced = False
            logger.error(f"Failed to sync benefit trigger after config change: {e}", exc_info=True)

    @staticmethod
    def get_payroll_payment_file_path(payroll_id, file_name=None):
        if file_name:
            return f"csv_reconciliation/payroll_{payroll_id}/{file_name}"
        return f"csv_reconciliation/payroll_{payroll_id}"
