import uuid
from django.test import override_settings
from payroll.models import Payroll, PayrollStatus, PayrollBenefitConsumption
from payroll.services import PayrollService
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
        
        payroll = Payroll(
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
        payroll.save(username='username_authorized')
        
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

    def test_create_payroll_persists_failed_status_on_bad_plan(self):
        """Omitting payment_cycle_id triggers DoesNotExist in _get_payment_cycle,
        which must persist the payroll with FAILED status.
        """
        name = "FailedCreationTest"
        Payroll.objects.filter(name=name).delete()

        service = PayrollService(self.user)
        result = service.create({
            "name": name,
            "payment_plan_id": str(self.payment_plan.id),
            "payment_point_id": str(self.payment_point.id),
            # payment_cycle_id intentionally omitted
            "payment_method": self.payment_method,
            "date_valid_from": self.date_valid_from,
            "date_valid_to": self.date_valid_to,
        })

        self.assertFalse(result.get("success", True), "Expected creation to fail")

        payroll = Payroll.objects.filter(name=name, is_deleted=False).first()
        self.assertIsNotNone(payroll, "Payroll row must persist even after failed benefit generation")
        self.assertEqual(payroll.status, PayrollStatus.FAILED)
        self.assertIn('creation_error', payroll.json_ext or {})
        self.assertIn('creation_params', payroll.json_ext or {})

    def test_retrigger_without_creation_params_returns_error(self):
        """Verify retrigger_creation returns an error when creation_params is absent."""
        name = "NoParamsRetriggerTest"
        Payroll.objects.filter(name=name).delete()

        payroll = Payroll(
            name=name,
            payment_plan=self.payment_plan,
            payment_cycle=self.payment_cycle,
            payment_point=self.payment_point,
            payment_method=self.payment_method,
            status=PayrollStatus.FAILED,
            date_valid_from=self.date_valid_from,
            date_valid_to=self.date_valid_to,
            json_ext={"creation_error": "original error"}  # No creation_params
        )
        payroll.save(username='username_authorized')

        service = PayrollService(self.user)
        result = service.retrigger_creation({"id": payroll.id})

        self.assertFalse(result.get("success", True), "Expected retrigger to return failure")
        payroll.refresh_from_db()
        self.assertEqual(payroll.status, PayrollStatus.FAILED, "Status must stay FAILED")
