# Privacy Policy

Version 0.1 · Effective {{EFFECTIVE_DATE}}

This policy explains what personal data the hosted DevOps AI Agent service collects, why, who receives it and how long we keep it. The controller for account, billing and website data is {{LEGAL_NAME}} ({{ENTITY_TYPE}}, {{LEGAL_ADDRESS}}). For the content you send to the Service, we are your processor; see the [Data Processing Addendum](/dpa). Contact: {{LEGAL_EMAIL}}.

## 1. What we collect

- **Account data:** the team or company name and email address you give at signup, an account ID, your plan, and when you accepted our terms and which version.
- **API keys:** we store only a one-way hash and a short prefix of each key, never the key itself. Recovery links are stored as hashes and expire after 30 minutes.
- **Usage data:** counts of analyze runs, cost checks and LLM tokens per account and time, used for limits and billing.
- **Billing data:** payments are handled by Stripe. We store your Stripe customer and subscription identifiers and the subscription status. We never see or store your full card number.
- **Slack link:** if you connect Slack, the ID of your Slack workspace, so we can match commands to your account.
- **Network data:** the IP address of signup and key-recovery requests, kept for rate limiting and deleted after 24 hours. Our hosting provider ({{HOSTING_PROVIDER}}) may keep standard server and security logs.
- **Content you send:** the question, logs and any Kubernetes, GitHub or cost information in your requests. Our product database does not store request content; it is processed to produce your response.
- **Browser storage:** the account page keeps your API key in your browser's session storage until you close the tab. We set no cookies and run no advertising or analytics trackers.

## 2. Why we use it, and our legal basis

- To provide the Service, run your account, apply limits and bill you (performance of a contract).
- To secure the Service, prevent abuse and fix problems (legitimate interests).
- To keep tax and accounting records and respond to lawful requests (legal obligation).
- To send you service emails such as key recovery and important changes to the Service or terms. We send no marketing email.

We do not sell personal data and do not use Your Content to train AI models.

## 3. Who receives it

- **Stripe** for payments and subscription management.
- **An AI model provider.** When AI diagnosis is switched on, the text needed to answer your request (your question, supplied logs and tool output) is sent to the provider we have configured, which is an OpenAI-compatible service such as OpenAI. When it is off, nothing is sent to an AI provider and a rule-based fallback is used.
- **An email provider**, for key-recovery messages.
- **Slack**, if you use the Slack integration.
- **{{HOSTING_PROVIDER}}**, which hosts the Service and its database.
- Professional advisers, and authorities where the law requires.

The current list of processors is also in Annex III of the [Data Processing Addendum](/dpa).

## 4. International transfers

Some of these providers process data outside your country. Where the law requires it, we rely on an adequacy decision or approved standard contractual clauses.

## 5. How long we keep it

- Account, usage and billing-reference data: while your account exists.
- Sign-up and recovery IP addresses: 24 hours. Recovery tokens: until they expire or are used.
- After you close your account we delete or anonymize your data within 30 days, except records we must keep for tax and accounting law, which we keep for the period the law requires. Stripe keeps its own records under its policy.

## 6. Security

Keys and recovery links are stored hashed, recovery links travel in the URL fragment so they are not sent to or logged by our server, and requests are rate limited. No system is perfectly secure; if a breach affects your personal data we will tell you and the authorities as the law requires.

## 7. Your rights

Depending on where you live, you may ask to access, correct, delete or export your personal data, to restrict or object to some uses, and to withdraw consent where we rely on it. Email {{LEGAL_EMAIL}}; we will reply within the time the law allows. You may also complain to your data protection authority.

## 8. Children

The Service is for business use and not for anyone under 18. We do not knowingly collect data from children.

## 9. Changes

We will post changes here and, for material ones, email the address on your account at least 30 days before they apply.

## 10. Contact

{{LEGAL_NAME}}, {{LEGAL_ADDRESS}}. Email: {{LEGAL_EMAIL}}.
