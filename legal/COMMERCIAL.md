# V.A.L.E.R.I.A. Licensing and Commercial Use

V.A.L.E.R.I.A. — Virtual Agentic Language-model Execution, Routing & Interface
Administrator — uses a three-part licensing model:

1. the V.A.L.E.R.I.A. Community License for source-available Community
   Components;
2. negotiated Commercial Licenses for external service, SaaS, OEM, and
   redistribution rights; and
3. negotiated Enterprise Licenses for separately identified proprietary
   Enterprise Components and services.

This page is an explanatory summary. It does not replace `LICENSE.md`, an
accepted Order Form, or a signed agreement. If this page conflicts with an
applicable legal agreement, the agreement controls.

## Why the project uses a custom Community License

The model was informed by the Sustainable Use License 1.0 and n8n's published
licensing explanations. The unmodified SUL 1.0 permits internal business and
non-commercial use but limits providing the software to third parties to free,
non-commercial distribution. V.A.L.E.R.I.A.'s intended model additionally needs
a precise permission for paid installation and configuration on a client's own
infrastructure, while reserving hosted customer access, commercial
redistribution, OEM, and white-label activity.

Those differences cannot be introduced while accurately continuing to call the
result SUL 1.0. The project therefore uses a separately named custom
`V.A.L.E.R.I.A. Community License 1.0`. It is source-available, not OSI-approved
open source.

Reference materials used for this comparison:

- [official n8n Sustainable Use License text](https://github.com/n8n-io/n8n/blob/master/LICENSE.md);
- [official n8n community license explanation](https://github.com/n8n-io/n8n-docs/blob/main/docs/n8n-community-license/README.md);
- [official n8n use-case guidance](https://support.n8n.io/article/can-i-use-your-license-for-my-use-case).

V.A.L.E.R.I.A. is not affiliated with or endorsed by n8n.

## Community use at a glance

The Community License permits, without a paid V.A.L.E.R.I.A. license:

- personal projects, learning, education, research, experiments, and hobbies;
- internal use by organizations of any size, including commercial organizations;
- internal development, automation, agent orchestration, infrastructure work,
  project management, testing, and CI/CD;
- producing and selling independent code, reports, analyses, and other work
  product created with assistance from V.A.L.E.R.I.A.;
- paid freelance and consulting work where the customer receives the result,
  not access to the platform;
- unlimited internal agents, managers, tasks, pipelines, users, projects,
  repositories, environments, servers, and local models;
- multiple self-hosted internal instances;
- private modifications, plugins, integrations, custom agents, and custom AI
  managers; and
- paid installation or configuration of a dedicated Client-Controlled
  Deployment when the client independently receives and complies with the
  Community License.

There is no Community fee based on organization size, revenue, employee count,
agent runs, tasks, generated tokens, or internal managers.

## When a Commercial License is required

A separate written Commercial License is required for activities such as:

- offering V.A.L.E.R.I.A. functionality as a hosted SaaS product;
- selling access to customer-facing management interfaces or APIs;
- managed hosting for external customers where the provider operates the
  platform;
- multi-tenant or provider-controlled single-tenant commercial services;
- selling or commercially redistributing V.A.L.E.R.I.A. or a modified version
  as a software product;
- embedding it in a commercially distributed product or appliance;
- OEM distribution, independent sublicensing, or resale; and
- commercial white-label deployment or removal of product branding.

The decisive question is generally not whether You earn revenue. It is whether
the customer receives access to, control of, or an independently deployable copy
of material V.A.L.E.R.I.A. functionality.

## Common scenarios

| Scenario | Default result |
| --- | --- |
| A company uses V.A.L.E.R.I.A. internally | Community use permitted |
| Agents internally produce code sold to clients | Community use permitted |
| A freelancer uses it to complete paid assignments | Community use permitted |
| A consultant installs it in a client-controlled account | Community use permitted if Section 5 of `LICENSE.md` is followed |
| A contractor operates it solely for one organization's internal use | Community use permitted; the organization remains responsible |
| A provider hosts a dedicated instance and sells access to a customer | Commercial License required |
| Customers manage agents through a UI, API, protocol, or embedded control | Commercial License required |
| Software containing V.A.L.E.R.I.A. is sold as an OEM product | Express OEM rights required |
| A modified version is sold under another brand | Commercial and express white-label rights required |
| A controlled group shares an internal deployment | Community use permitted if each entity complies |

Machine-to-machine access is not a loophole: a customer-facing API that exposes
material platform functionality is treated like a customer-facing user
interface. Conversely, delivering a static report, code, or another independent
result does not become SaaS merely because V.A.L.E.R.I.A. helped create it.

## Commercial License structure

Commercial rights are defined by `legal/LICENSE_COMMERCIAL.md` and an accepted
`legal/COMMERCIAL_ORDER_FORM.md`. The standard commercial agreement can cover:

- SaaS and managed hosting;
- Authorized Customers and External Users;
- commercial APIs and customer-facing agent management;
- specified deployments and customer installations;
- permitted modifications; and
- expressly negotiated OEM, distribution, source-transfer, or white-label
  rights.

The proposed default term is 12 months. An illustrative starting point of EUR
199 per month may be used for discussion, but it is not a binding offer or
universal price. Actual fees, metrics, renewal, support, and scope exist only in
an accepted Order Form. Agent runs, task count, token count, and internal manager
count are not fee metrics by default.

Hosted customer access does not require giving customers independent
sublicensing rights. Standard hosted-service permission lets Authorized Users
access the service while the licensee retains operational control. Source
transfer, independently deployable copies, redistribution, and OEM sublicensing
require separate express grants.

## Enterprise Licensing

Enterprise licensing applies only to components expressly identified as
Enterprise Components and to negotiated services such as priority support or an
SLA. Potential features include SSO/SAML, advanced RBAC, centralized audit logs,
security policy management, compliance tooling, multi-instance administration,
advanced organization management, and premium integrations.

Enterprise pricing is negotiated. Ordinary internal use of Community Components
does not require an Enterprise License, and releasing an Enterprise edition does
not narrow an existing Community grant.

## Third-party software

Third-party components do not become subject to V.A.L.E.R.I.A.'s custom license.
In particular, this repository currently includes a separately licensed AGPL
component under `mcp/opencode-dcp-local/` and a web interface bundle with the
notices of its third-party packages (`src/opencode_web/static/3rdpartylicenses.txt`).
See `NOTICE.md`. A distributor must complete a dependency and license review for
the exact artifact it ships.

## Requesting commercial terms

Commercial licensing contact: **[COMMERCIAL LICENSING CONTACT TO BE ADDED]**

To request terms, provide the intended deployment model, whether customers will
receive UI or API access, number and type of deployments, expected external-user
scope, desired term, support needs, and any OEM, redistribution, source-delivery,
or white-label requirements.

No commercial permission is granted until the applicable agreement and Order
Form are validly accepted.
