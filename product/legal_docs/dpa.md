# Data Processing Addendum

Version 0.1 · Effective {{EFFECTIVE_DATE}}

This addendum forms part of the [Terms of Service](/terms) between {{LEGAL_NAME}} ("Processor") and the customer ("Controller") and applies automatically when the Service processes personal data that is subject to data protection law, such as the EU or UK GDPR. If you need a countersigned copy, email {{LEGAL_EMAIL}}.

## 1. Scope and roles

Controller determines why and how personal data in Your Content is processed. Processor processes it only on Controller's behalf to provide the Service. The subject matter, duration, nature, purpose, data types and data subjects are in Annex I.

## 2. Processor's obligations

- Process personal data only on Controller's documented instructions, which are the Terms, this addendum and Controller's use of the Service, unless the law requires otherwise (then Processor tells Controller first where permitted).
- Make sure everyone with access is bound by confidentiality.
- Apply the security measures in Annex II.
- Help Controller respond to data subject requests and meet its security, breach-notification and impact-assessment duties, taking account of the nature of processing and the information Processor has.
- Tell Controller without undue delay, and in any event within 72 hours, after becoming aware of a personal data breach affecting Controller's data, with the information then available.
- Tell Controller if it believes an instruction breaches data protection law.

## 3. Subprocessors

Controller gives general authorization for the subprocessors in Annex III. Processor will give at least 30 days' notice by email of any addition or replacement, during which Controller may object on reasonable data protection grounds; if the parties cannot resolve it, Controller may terminate the affected Service and receive a refund of prepaid fees for the remaining period. Processor binds each subprocessor to obligations no less protective than these and remains responsible for them.

## 4. International transfers

Personal data may be transferred outside the EEA, the UK or Switzerland only with a valid transfer mechanism. Where required, the parties incorporate the EU Standard Contractual Clauses (Module Two, controller to processor; and Module Three for onward transfers), and the UK Addendum, as follows: the Annexes below are Annexes I to III of the clauses; the competent supervisory authority is that of Controller's establishment; governing law and forum for the clauses are those of an EU Member State chosen by the parties or, absent a choice, Ireland.

## 5. Return and deletion

Processor does not keep request content in its product database. On termination Processor deletes remaining personal data of Controller within 30 days, except where the law requires retention, and confirms deletion on request.

## 6. Audits

On reasonable notice and no more than once a year (unless a breach or authority requires otherwise), Processor will provide the information needed to show compliance and allow audits by Controller or its auditor, subject to confidentiality and without access to other customers' data.

## 7. Liability and precedence

Liability under this addendum is subject to the limits in the Terms, except where the law or the Standard Contractual Clauses provide otherwise. If this addendum conflicts with the Terms on personal data, this addendum prevails; the Standard Contractual Clauses prevail over both.

## Annex I: Description of processing

- **Subject matter and purpose:** operating the DevOps AI Agent service: analyzing incident descriptions, logs and related infrastructure information and returning diagnoses and suggestions.
- **Duration:** the term of the agreement.
- **Nature of processing:** receipt, transient processing in memory, transmission to the AI model provider when AI diagnosis is on, return of results, and metering of usage counts.
- **Types of personal data:** whatever Controller includes in requests. This is expected to be limited to technical identifiers that appear in logs, such as usernames, email addresses, IP addresses and hostnames. Controller must not send special-category data or secrets.
- **Data subjects:** Controller's personnel and users and other individuals who appear in Controller's logs.
- **Frequency:** continuous, per request.

## Annex II: Security measures

- API keys and recovery tokens stored only as one-way hashes.
- Per-account authentication, plan limits and rate limiting; per-IP limits on signup and recovery.
- Request content is not written to the product database.
- Recovery links delivered in the URL fragment and valid for 30 minutes, single use.
- Webhook and Slack requests verified by signature.
- Access to production systems restricted to authorized personnel; encrypted transport (TLS) for the Service's public endpoints.

## Annex III: Subprocessors

- **{{HOSTING_PROVIDER}}:** hosting and database.
- **Stripe:** payments (account and billing identifiers only, not request content).
- **AI model provider, when AI diagnosis is on:** an OpenAI-compatible service such as OpenAI, which receives the text of requests.
- **Email delivery provider:** key-recovery email (email address only).
- **Slack, when Controller uses the Slack integration:** command delivery.
