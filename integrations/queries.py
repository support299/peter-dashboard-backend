"""
Pinned Jobber GraphQL selections for JOBBER_GRAPHQL_VERSION.

Money fields: never query deprecated `cost`. Use `amounts { total }` on
invoices/quotes, and Job's current `total` scalar. Always send
X-JOBBER-GRAPHQL-VERSION; bump the env value only after changelog review.

Rate limits: every connection MUST pass first/last. Nested collections stay
small so requestedQueryCost stays under maximumAvailable (10000). Do not nest
visits under jobs in list sync.
"""

ACCOUNT_QUERY = """
query GetAccount {
  account {
    id
    name
    countryCode
    createdAt
    phone
  }
}
"""

QUERY_FIELDS_INTROSPECTION = """
query QueryRootFields {
  __type(name: "Query") {
    fields(includeDeprecated: true) {
      name
      description
      isDeprecated
      deprecationReason
      args {
        name
        type {
          kind
          name
          ofType { kind name ofType { kind name } }
        }
      }
    }
  }
}
"""

TYPE_INTROSPECTION = """
query TypeShape($name: String!) {
  __type(name: $name) {
    name
    kind
    description
    fields(includeDeprecated: true) {
      name
      description
      isDeprecated
      deprecationReason
      args { name }
      type {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType { kind name }
          }
        }
      }
    }
  }
}
"""

USERS_SELECTION = """
id
status
isAccountAdmin
isAccountOwner
availableForScheduling
createdAt
lastLoginAt
assignedColor
name { first last full }
email { raw }
"""

CLIENTS_SELECTION = """
id
name
firstName
lastName
companyName
isCompany
isLead
isArchived
balance
email
phone
createdAt
updatedAt
jobberWebUri
billingAddress {
  city
  street
  street1
  street2
  postalCode
  province
  country
}
"""

PROPERTIES_SELECTION = """
id
name
isBillingAddress
jobberWebUri
client { id }
address {
  street
  street1
  street2
  city
  province
  postalCode
  country
}
"""

JOBS_SELECTION = """
id
jobNumber
title
jobStatus
jobType
billingType
total
invoicedTotal
uninvoicedTotal
completedAndUninvoicedVisitsCount
completedAndUninvoicedVisitsTotal
startAt
endAt
completedAt
createdAt
updatedAt
instructions
jobberWebUri
defaultVisitTitle
client { id }
property { id }
salesperson { id }
source
visitsInfo {
  firstVisit { id startAt }
}
"""

TASKS_SELECTION = """
id
title
instructions
startAt
endAt
isComplete
client { id }
assignedUsers(first: 5) {
  nodes { id }
}
"""

VISITS_SELECTION = """
id
title
visitStatus
isComplete
allDay
duration
startAt
endAt
completedAt
completedBy
createdAt
clientConfirmed
isLastScheduledVisit
instructions
client { id }
job { id }
property { id }
invoice { id }
assignedUsers(first: 10) {
  nodes { id }
}
lineItems(first: 10) {
  nodes {
    id
    name
    description
    quantity
    unitPrice
    totalPrice
  }
}
"""

INVOICES_SELECTION = """
id
invoiceNumber
invoiceStatus
subject
issuedDate
dueDate
createdAt
updatedAt
jobberWebUri
client { id }
amounts {
  total
  subtotal
  taxAmount
  invoiceBalance
  paymentsTotal
  depositAmount
  discountAmount
}
jobs(first: 10) {
  nodes { id }
}
lineItems(first: 10) {
  nodes {
    id
    name
    description
    quantity
    unitPrice
    totalPrice
  }
}
"""

APP_DISCONNECT = """
mutation Disconnect {
  appDisconnect {
    app { name author }
    userErrors { message }
  }
}
"""

TYPES_TO_INTROSPECT = [
    "Query",
    "Account",
    "Client",
    "ClientAddress",
    "Job",
    "Visit",
    "Invoice",
    "InvoiceAmounts",
    "User",
    "Name",
    "UserEmail",
    "Property",
    "PropertyAddress",
    "JobLineItem",
    "InvoiceLineItem",
    "Quote",
    "QuoteAmounts",
    "VisitsInfo",
    "VisitSchedule",
    "JobStatusTypeEnum",
    "JobTypeTypeEnum",
    "VisitStatusTypeEnum",
    "InvoiceStatusTypeEnum",
    "CustomFieldUnion",
    "WebHookTopicEnum",
    "WebHookPayload",
    "Task",
]


def node_query(root_field: str, selection: str) -> str:
    return f"""
    query WebhookNode($id: EncodedId!) {{
      {root_field}(id: $id) {{
        {selection}
      }}
    }}
    """

