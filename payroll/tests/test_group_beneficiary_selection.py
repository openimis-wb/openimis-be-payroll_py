"""PayrollService must build its beneficiary queryset from the model that
matches the benefit plan type.

A GROUP-type plan is served by GroupBeneficiary. Selecting from Beneficiary
instead yields an empty queryset and, since the calculation strategies select
related fields belonging to their own model, raises

    FieldError: Invalid field name(s) given in select_related: 'group'

It also silently disables filter_criteria.location_ids for GROUP plans, because
the location path differs between the two models.
"""
import json
import uuid
from datetime import datetime, timedelta

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from contribution_plan.models import PaymentPlan
from core.test_helpers import LogInHelper
from individual.models import Group, GroupIndividual, Individual
from location.test_helpers import create_test_location
from payroll.services import PayrollService
from social_protection.models import (Beneficiary, BeneficiaryStatus, BenefitPlan,
                                      GroupBeneficiary)

STOCK_RULE = "32d96b58-898a-460a-b357-5fd4b95cd87c"


class GroupBeneficiarySelectionTests(TestCase):
    IN_SCOPE = 3
    OUT_OF_SCOPE = 2

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username='admin_group_select')
        now = datetime.now()
        tag = uuid.uuid4().hex[:5]

        cls.commune = create_test_location("W", custom_props={"code": f"CW{tag[:4]}"})
        cls.village = create_test_location(
            "V", custom_props={"code": f"CV{tag[:4]}", "parent": cls.commune})
        cls.other_village = create_test_location("V", custom_props={"code": f"OV{tag[:4]}"})

        cls.group_plan = BenefitPlan(
            code=f"GS{tag}", name="Group Selection BP",
            type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
            date_valid_from=now - timedelta(days=1),
            date_valid_to=now + timedelta(days=365),
        )
        cls.group_plan.save(user=cls.user)

        for i in range(cls.IN_SCOPE + cls.OUT_OF_SCOPE):
            location = cls.village if i < cls.IN_SCOPE else cls.other_village
            ind = Individual(first_name=f"GS{i}", last_name=tag, dob="1990-01-01")
            ind.save(user=cls.user)
            grp = Group(code=f"G{tag}{i}", location=location)
            grp.save(user=cls.user)
            GroupIndividual(group=grp, individual=ind, recipient_type="PRIMARY").save(user=cls.user)
            GroupBeneficiary(group=grp, benefit_plan=cls.group_plan,
                             status=BeneficiaryStatus.ACTIVE).save(user=cls.user)

        cls.payment_plan = PaymentPlan(
            code=f"GSP{tag}", name="Group Selection PP",
            benefit_plan_id=cls.group_plan.id,
            benefit_plan_type=ContentType.objects.get_for_model(BenefitPlan),
            calculation=STOCK_RULE, periodicity=1,
            date_valid_from=now - timedelta(days=1),
            date_valid_to=now + timedelta(days=365),
            json_ext={"calculation_rule": {"fixed_batch": "1000",
                                           "limit_per_single_transaction": ""}},
        )
        cls.payment_plan.save(user=cls.user)

    def _select(self, criteria=None):
        obj_data = {}
        if criteria is not None:
            obj_data["json_ext"] = json.dumps({"filter_criteria": criteria})
        return PayrollService(self.user)._select_beneficiary_based_on_criteria(
            obj_data, self.payment_plan)

    def test_group_plan_selects_group_beneficiaries(self):
        qs = self._select()
        self.assertIs(qs.model, GroupBeneficiary,
                      "a GROUP-type plan must be served by GroupBeneficiary")
        self.assertEqual(qs.count(), self.IN_SCOPE + self.OUT_OF_SCOPE)

    def test_group_plan_does_not_select_individual_beneficiaries(self):
        self.assertEqual(
            Beneficiary.objects.filter(benefit_plan=self.group_plan).count(), 0,
            "sanity: this plan has no individual beneficiaries, so selecting "
            "Beneficiary would silently return nothing")

    def test_location_filter_scopes_group_plan(self):
        """location_ids must apply through group__location for GROUP plans."""
        qs = self._select({"location_ids": [str(self.commune.uuid)]})
        self.assertEqual(
            qs.count(), self.IN_SCOPE,
            "location_ids must scope a GROUP plan via group__location; before the "
            "fix the filter matched nothing and was effectively ignored")
