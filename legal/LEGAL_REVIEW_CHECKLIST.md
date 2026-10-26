# V.A.L.E.R.I.A. Legal Review Checklist

Status: draft package requiring professional legal review before publication,
public contribution intake, or commercial signature.

This checklist records unresolved decisions and validation work. Checking an
item should mean that qualified counsel or the responsible compliance owner has
reviewed supporting evidence, not merely that text exists.

## 1. License architecture and Community terms

- [ ] Confirm that the custom `V.A.L.E.R.I.A. Community License 1.0` expresses
  the intended source-available model and should not use an established standard
  license instead.
- [ ] Confirm that the license is clearly presented as non-OSI source-available
  and that no repository page calls it open source.
- [ ] Review the comparison with the official Sustainable Use License 1.0 and
  n8n guidance; confirm that no protected text, name, or endorsement is implied.
- [ ] Test the Internal Use, Personnel, Controlled Affiliate, External User,
  Hosted Service, Commercial Product Offering, and Client-Controlled Deployment
  definitions against expected business scenarios.
- [ ] Confirm that internal commercial use remains free for organizations of any
  size and that there are no accidental company-size, revenue, employee,
  agent, task, token, user, project, repository, server, or model limits.
- [ ] Confirm that output-based commercial productivity, freelancing, consulting,
  and client-result delivery remain permitted.
- [ ] Confirm that paid installation and configuration on client-controlled
  infrastructure are permitted without accidentally authorizing resale,
  sublicensing, or provider-controlled managed hosting.
- [ ] Review whether non-commercial public hosting should be expressly allowed,
  prohibited, or handled case by case; the current Hosted Service restriction
  can cover free external platform access.
- [ ] Review whether the source-delivery requirement for permitted client
  installations is commercially and operationally intended.
- [ ] Determine whether a formal versioning and change policy for future Community
  License revisions is needed.

## 2. Edge cases and technical boundaries

- [ ] Produce written decisions for contractors, outsourcing providers,
  subsidiaries, affiliates, joint ventures, franchises, and shared-service
  organizations.
- [ ] Validate how control is allocated when infrastructure is purchased by a
  client but administered through a provider account.
- [ ] Distinguish client-controlled deployment, managed service, hosted service,
  consulting, support, and business-process outsourcing using actual deployment
  diagrams.
- [ ] Define when customer-facing APIs, MCP endpoints, webhooks, embedded widgets,
  dashboards, and indirect automation expose "material functionality."
- [ ] Validate treatment of single-tenant managed hosting versus multi-tenant
  hosting.
- [ ] Review plugins, separate processes, network integrations, and custom agents
  for derivative-work and distribution implications; do not make categorical
  claims unsupported by applicable law.
- [ ] Decide whether evaluation, demonstration, trial, academic hosting, or public
  non-commercial instances need express additional permissions.

## 3. Copyright and provenance

- [ ] Confirm documentary evidence that Alexey Khamitsevich owns all rights
  required to license the original V.A.L.E.R.I.A. materials identified as such.
- [ ] Audit prior employers, clients, contractors, generated code, copied snippets,
  and collaboration history for competing ownership or confidentiality claims.
- [ ] Identify all independent contributions and verify an assignment, CLA, or
  compatible inbound license for each; do not describe independent contributor
  copyright as owned by the Licensor.
- [ ] Decide whether copyright headers should be added to original source files
  without altering third-party headers.
- [ ] Confirm that `NOTICE.md` accurately separates original and third-party
  material and preserves all required notices.
- [ ] Review whether V.A.L.E.R.I.A. naming creates trademark risks; no registration
  or exclusive trademark right is asserted by these drafts.
- [ ] Establish an archival process for source versions, license versions, CLA
  records, and accepted Order Forms.

## 4. Third-party and open-source compliance

- [ ] Generate an SBOM and complete license inventory for source, Python, npm,
  container, OS-package, browser, runtime, test, and build dependencies.
- [ ] Verify every dependency license from authoritative upstream materials; do
  not rely solely on package-registry metadata.
- [ ] Review `mcp/opencode-dcp-local/`, declared `AGPL-3.0-or-later`, including
  provenance, modifications, notices, source availability, installation and
  network interaction, and whether any original V.A.L.E.R.I.A. material forms a
  derivative or combined work subject to AGPL obligations.
- [ ] Preserve the complete AGPL text and all upstream attribution for that
  directory; identify local modifications and dates if required.
- [ ] Verify the packages bundled into the web interface build and preserve their
  notices (`src/opencode_web/static/3rdpartylicenses.txt`) in distributed artifacts.
- [ ] Review licenses and notices for OpenCode and all MCP servers, SDKs, models,
  tokenizers, fonts, icons, JavaScript assets, and container contents.
- [ ] Distinguish API integration from bundling, binary redistribution, source
  modification, and incorporation for OpenCode, GitLab, GitHub, YouTrack, LLM
  providers, database engines, and container tooling.
- [ ] Build artifact-specific notice files and source offers where required.
- [ ] Add an automated dependency/license policy and release compliance gate.
- [ ] Confirm that Commercial and Enterprise grants never promise rights the
  Licensor lacks in Third-Party Materials.

## 5. Contributor License Agreement

- [ ] Decide whether a CLA is necessary for the contribution strategy and
  commercial relicensing objectives; consider a Developer Certificate of Origin
  as an alternative only after evaluating the relicensing gap.
- [ ] Review the breadth and enforceability of the copyright, sublicensing,
  relicensing, patent, moral-rights, and feedback provisions.
- [ ] Complete governing-law and dispute-venue fields before accepting signed
  CLA counterparts.
- [ ] Validate that pull-request text constitutes valid, informed electronic
  acceptance and produces durable evidence linking identity, exact CLA version,
  timestamp, and contribution.
- [ ] Create an entity/employer CLA or authorization workflow where rights are
  employer-owned.
- [ ] Define procedures for minors, multiple authors, bots, generated code,
  pseudonymous accounts, and contributions incorporating third-party material.
- [ ] Define rejection, remediation, and takedown procedures for defective
  provenance.
- [ ] Publish a privacy notice and retention schedule for contributor identity
  and signature records.

## 6. Commercial and OEM agreement

- [ ] Identify the legal contracting party and complete valid licensing, notice,
  billing, and invoicing contacts without publishing unnecessary personal data.
- [ ] Review the 12-month default, renewal mechanics, cancellation, refunds,
  suspension, non-payment, insolvency, and 30-day cure provisions.
- [ ] Confirm that each Order Form unambiguously identifies Licensed Software,
  versions, Authorized Deployments, Customers, External Users, interfaces, field
  of use, and rights granted.
- [ ] Decide standard pricing and currency; retain EUR 199/month only as a clearly
  non-binding illustration if commercially desired.
- [ ] Confirm that task, token, agent-execution, and internal-manager counts are
  not fee metrics unless an Order Form expressly overrides the default.
- [ ] Review usage reporting, record retention, audit frequency, underpayment
  threshold, and confidentiality for proportionality and enforceability.
- [ ] Create separate OEM terms for binaries, source, recipient hosting,
  modifications, updates, copy limits, royalties, sublicenses, end-of-term copies,
  and channel partners.
- [ ] Define white-label branding permissions separately from non-removable legal
  notices.
- [ ] Confirm that authorized hosted end-user access is not accidentally treated
  as prohibited independent sublicensing.
- [ ] Decide whether source escrow, business continuity, change-of-control, most
  favored pricing, or procurement clauses are supportable.

## 7. Enterprise terms and product separation

- [ ] Define and technically label Enterprise Components so customers can
  distinguish them from Community Components.
- [ ] Verify that license enforcement cannot disable or narrow Community rights
  after Enterprise expiration.
- [ ] Define negotiated internal and external user metrics without creating
  mandatory Enterprise fees for ordinary Community internal use.
- [ ] Prepare support, maintenance, version support, security response,
  end-of-life, and SLA schedules before making service promises.
- [ ] Validate source-access, modification, disaster-recovery, and test-instance
  policies for Enterprise Components.

## 8. AI safety, warranties, and liability

- [ ] Map actual agent capabilities: shell access, file deletion, repository
  writes, commits, pushes, CI/CD, secrets, network, infrastructure, external APIs,
  and autonomous continuation.
- [ ] Align product warnings, default permissions, documentation, logging,
  approval gates, backups, and isolation with contractual risk allocation.
- [ ] Review all AS-IS, merchantability, fitness, title, non-infringement,
  security, availability, AI-output, continuity, and compliance disclaimers for
  the selected governing law and customer type.
- [ ] Review the paid liability cap of fees paid in the preceding 12
  months and whether separate caps are needed for data, security, IP, support,
  confidentiality, or indemnities.
- [ ] Confirm mandatory carve-outs for fraud, willful misconduct, death, personal
  injury, consumer protections, gross negligence, data protection, and other
  non-excludable liability under applicable law.
- [ ] Decide whether Licensor can offer any IP indemnity; none is granted by
  default.
- [ ] Confirm insurance requirements and whether autonomous-agent risks are
  covered.

## 9. Privacy, security, and regulated use

- [ ] Identify controller, processor, and subprocessor roles for each hosted or
  support model.
- [ ] Prepare a data processing agreement, privacy notice, security schedule,
  subprocessor list, transfer mechanism, breach process, retention policy, and
  deletion process where applicable.
- [ ] Define Customer Data export format, availability, costs, retention, and
  deletion after suspension or termination.
- [ ] Review credentials, telemetry, logs, model prompts, repositories, secrets,
  backups, and support access for personal or confidential data.
- [ ] Assess AI-specific transparency, human oversight, risk management,
  recordkeeping, and prohibited-use obligations applicable to target markets and
  use cases.
- [ ] Review export controls, sanctions, accessibility, cybersecurity,
  procurement, sector regulation, and consumer law for each offering.

## 10. Governing law, disputes, and formalities

- [ ] Replace **[GOVERNING LAW TO BE DETERMINED]** consistently after counsel
  selects applicable law.
- [ ] Replace **[DISPUTE RESOLUTION VENUE TO BE DETERMINED]** consistently and
  decide between courts, arbitration, mediation, or escalation procedures.
- [ ] Add transaction-appropriate notice mechanics and valid service details.
- [ ] Review electronic signature, clickwrap, browsewrap, language, counterpart,
  authority, and record-retention requirements.
- [ ] Determine whether consumer, small-business, public-sector, or local-language
  mandatory rules require alternative terms.
- [ ] Review severability, assignment, force majeure, waiver, precedence, entire
  agreement, and survival language.

## 11. Payments, tax, and operations

- [ ] Determine who invoices and receives payments and complete lawful payment
  instructions outside the public template where appropriate.
- [ ] Review indirect taxes, withholding, tax residency documentation, invoicing
  content, currency conversion, late-payment rules, and refund obligations for
  each transaction.
- [ ] Confirm whether public pricing creates advertising, consumer, procurement,
  or offer obligations; preserve the non-binding nature of illustrations.
- [ ] Establish an approval workflow so sales statements, proposals, and support
  commitments do not exceed the signed Order Form.
- [ ] Create a license register tracking versions, customers, scope, renewals,
  expirations, notices, and post-termination obligations.

## 12. Publication gate

- [ ] Run a repository-wide check for inconsistent project names, obsolete
  license claims, unauthorized ownership claims, and missing notices.
- [ ] Confirm every requested file exists and cross-links resolve.
- [ ] Confirm the exact notice appears consistently:
  `Copyright (c) 2026 Alexey Khamitsevich`.
- [ ] Ensure no documentation promises broader rights than `LICENSE.md`.
- [ ] Ensure no FAQ or summary adds restrictions absent from the legal text.
- [ ] Have qualified licensing, contract, privacy, tax, and applicable regulatory
  counsel approve the finalized documents for intended distribution and sales.
- [ ] Record approval date, reviewed commit, license version, and future-review
  owner: [TO BE COMPLETED].
