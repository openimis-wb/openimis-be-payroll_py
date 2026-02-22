from datetime import date


gql_payment_point_query = """
query q1 {
  paymentPoint {
    edges {
      node {
        id
      }
    }
  }
}
"""

gql_payment_point_filter = """
query q1 {
  paymentPoint(name_Iexact: "%s", location_Uuid: "%s", ppm_Uuid: "%s") {
    edges {
      node {
        id
      }
    }
  }
}
"""

gql_payment_point_create = """
mutation m1 {
  createPaymentPoint (input:{
    name: %s,
    locationId: %s,
    ppmId: "%s"
  }) {
    clientMutationId
  }
}
"""

gql_payment_point_update = """
mutation m1 {
  updatePaymentPoint (input:{
    id: %s
    name: %s,
    locationId: %s,
    ppmId: "%s"
  }) {
    clientMutationId
  }
}
"""

gql_payment_point_delete = """
mutation m1 {
  deletePaymentPoint (input:{
    ids: %s
  }) {
    clientMutationId
  }
}
"""

gql_payroll_query = """
query q2 {
  payroll {
    edges {
      node {
        id
      }
    }
  }
}
"""

gql_payroll_filter = """
query q2 {
  paymentPoint(name_Iexact: "%s",
                paymentPlan_Uuid: "%s",
                paymentPoint_Uuid: "%s"
                dateValidFrom: "%s"
                dateValidTo: "%s") {
    edges {
      node {
        id
      }
    }
  }
}
"""

gql_payroll_create = """
mutation createPayroll($name: String!, $paymentCycleId: UUID, $paymentPlanId: UUID!, $paymentPointId: UUID, $paymentMethod: String!, $status: PayrollStatusEnum!, $dateValidFrom: Date, $dateValidTo: Date, $jsonExt: JSONString, $clientMutationId: String) {
  createPayroll(input: {name: $name, paymentCycleId: $paymentCycleId, paymentPlanId: $paymentPlanId, paymentPointId: $paymentPointId, paymentMethod: $paymentMethod, status: $status, dateValidFrom: $dateValidFrom, dateValidTo: $dateValidTo, jsonExt: $jsonExt, clientMutationId : $clientMutationId}) {
    clientMutationId
  }
}
"""

gql_payroll_create_no_json_ext = """
mutation m2 {
  createPayroll (input:{
                name: "%s"
                paymentCycleId: "%s"
                paymentPlanId: "%s"
                paymentPointId: "%s"
                paymentMethod: "%s"
                status: %s
                dateValidFrom: "%s"
                dateValidTo: "%s"
  }) {
    clientMutationId
  }
}
"""


gql_payroll_delete = """
mutation m2 {
  deletePayroll (input:{
    ids: %s
  }) {
    clientMutationId
  }
}
"""

benefit_consumption_data_test = {
    "photo": "photo-test.jpg",
    "code": "BC123-TEST",
    "date_due": date(2023, 5, 31),
    "receipt": "REC-BC123-TEST",
    "amount": 500.00,
    "type": "Cash",
    "status": "ACCEPTED",
}


benefit_consumption_data_update = {
    "code": "BC123-fixed-fix",
}



gql_benefit_consumption_query = """
query q2 {
  benefitConsumption {
    edges {
      node {
        id
      }
    }
  }
}
"""


gql_payroll_retrigger = """
mutation retriggerPayroll($id: UUID!) {
  retriggerPayroll(input: {id: $id}) {
    clientMutationId
  }
}
"""
