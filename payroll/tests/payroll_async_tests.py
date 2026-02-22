import json
import uuid
from django.test import override_settings
from payroll.models import Payroll, PayrollStatus, PayrollBenefitConsumption
from payroll.tests.payroll_gql_tests import PayrollGQLTestCase
from payroll.tests.data import gql_payroll_create, gql_payroll_retrigger

@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class PayrollAsyncTests(PayrollGQLTestCase):
    
    def test_create_payroll_async_success(self):
        """Verify that creating a payroll triggers the async task and completes successfully."""
        name = "AsyncPayrollTest"
        Payroll.objects.filter(name=name).delete()
        
        variables = {
            "name": name,
            "paymentCycleId": str(self.payment_cycle.id),
            "paymentPlanId": str(self.payment_plan.id),
            "paymentPointId": str(self.payment_point.id),
            "paymentMethod": self.payment_method,
            "status": "PENDING_APPROVAL",
            "dateValidFrom": self.date_valid_from,
            "dateValidTo": self.date_valid_to,
            "jsonExt": self.json_ext_able_bodied_true,
            "clientMutationId": str(uuid.uuid4())
        }
        
        output = self.gql_client.execute(
            gql_payroll_create, 
            context=self.gql_context.get_request(), 
            variable_values=variables
        )
        self.assertIsNone(output.get('errors'), f"Mutation errors: {output.get('errors')}")
        
        payroll = Payroll.objects.get(name=name)
        self.assertEqual(payroll.status, PayrollStatus.PENDING_APPROVAL)
        
        benefit_count = PayrollBenefitConsumption.objects.filter(payroll=payroll).count()
        self.assertGreater(benefit_count, 0, "No benefits were created by the async task")

    def test_retrigger_payroll_creation(self):
        """Verify that retriggering a failed payroll restarts the creation process."""
        name = "RetriggerPayrollTest"
        Payroll.objects.filter(name=name).delete()

        creation_params = {
            "name": name,
            "payment_cycle_id": str(self.payment_cycle.id),
            "payment_plan_id": str(self.payment_plan.id),
            "payment_point_id": str(self.payment_point.id),
            "payment_method": self.payment_method,
            "date_valid_from": self.date_valid_from,
            "date_valid_to": self.date_valid_to,
            "json_ext": self.json_ext_able_bodied_true,
        }
        
        payroll = Payroll.objects.create(
            name=name,
            payment_cycle=self.payment_cycle,
            payment_plan=self.payment_plan,
            payment_point=self.payment_point,
            payment_method=self.payment_method,
            status=PayrollStatus.FAILED,
            date_valid_from=self.date_valid_from,
            date_valid_to=self.date_valid_to,
            json_ext={
                "creation_params": creation_params,
                "creation_error": "Some old error"
            }
        )
        
        variables = {"id": str(payroll.id)}
        output = self.gql_client.execute(
            gql_payroll_retrigger, 
            context=self.gql_context.get_request(), 
            variable_values=variables
        )
        self.assertIsNone(output.get('errors'), f"Retrigger errors: {output.get('errors')}")
        
        payroll.refresh_from_db()
        self.assertEqual(payroll.status, PayrollStatus.PENDING_APPROVAL)
        self.assertNotIn('creation_error', payroll.json_ext or {})
        
        benefit_count = PayrollBenefitConsumption.objects.filter(payroll=payroll).count()
        self.assertGreater(benefit_count, 0, "No benefits were created after retriggering")
