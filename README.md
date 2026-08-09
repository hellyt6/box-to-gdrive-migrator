# box-to-gdrive-migrator

Identity-aware file migration from Box to Google Drive, using Okta as the
source of truth for user matching instead of email address.

> **Status: designed, not deployed.** I scoped and built this while at
> Invitae to support a Box → Google Workspace consolidation, but the
> Terraform Cloud subscription needed to run it in our environment wasn't
> approved before I moved on. This repo is the architecture and working
> code as I built it, published here as a portfolio piece — not a
> battle-tested production tool.

## The problem

When migrating file storage between SaaS platforms, the naive approach is
to match user accounts by email address. In practice this breaks constantly:

- A user's Box account was provisioned under a personal alias
  (`j.doe@company.com`) while their Google Workspace account uses their
  canonical address (`jane.doe@company.com`)
- Name changes, department transfers, or contractor-to-FTE conversions
  leave stale email records on one platform but not the other
- Matching on email silently creates a **new** Google Drive account/folder
  structure instead of merging into the right one, which is how you end up
  with duplicate identities and orphaned files

Since the company already ran Okta as the identity source of truth for
every user, the more reliable fix was to stop matching on email entirely
and instead resolve both platform identities through a stable identifier
in Okta.

## How it works

1. **Okta is queried first.** Each user's Okta profile carries two custom
   attributes — `boxUserId` and `googleWorkspaceId` — populated during
   provisioning. This gives a durable link between the two platform
   identities that doesn't depend on email matching at migration time.
2. **Box is queried** for each mapped user's files using the admin
   as-user capability.
3. **Google Drive is queried** for the matching Workspace user via a
   domain-wide-delegated service account, so files land directly in the
   correct destination account.
4. **A manifest (DynamoDB or local JSON in dev)** tracks migrated file
   IDs and checksums, so re-running the job is idempotent and doesn't
   create duplicates on retry.
5. **Infrastructure is provisioned with Terraform**: a scheduled Lambda
   function, a least-privilege IAM execution role, and Secrets Manager
   entries for the three sets of API credentials (Okta, Box, Google).

```
Okta (source of truth)
   |  boxUserId + googleWorkspaceId per user
   v
migrate.py  --lists Box files as user-->  Box API
   |
   |--uploads to matched user's Drive-->  Google Drive API (domain-wide delegation)
   |
   `--writes migration manifest-------->  DynamoDB (dedupe/idempotency)
```

## Repo layout

```
terraform/       AWS infrastructure (Lambda, IAM role, Secrets Manager, EventBridge schedule)
src/migrate.py   Migration logic
requirements.txt Python dependencies
```

## Why Okta as the join key, not email

This is the part I'd defend in an interview: identity platforms like Okta
already do the hard work of maintaining a canonical user record across
every downstream SaaS app. Re-deriving that mapping from two systems that
weren't designed to agree with each other (Box and Google Workspace) is
redundant and error-prone. Treating Okta as the join key turns a fuzzy
matching problem into a deterministic lookup.

## Setup (for anyone actually running this)

1. `cp terraform/terraform.tfvars.example terraform/terraform.tfvars` and
   fill in your AWS region, Okta org URL, and notification email.
2. Store your Okta API token, Box JWT service account config, and Google
   service account key in Secrets Manager under the names referenced in
   `terraform/main.tf`.
3. `terraform init && terraform plan` from `terraform/`.
4. `pip install -r requirements.txt` for local testing of `src/migrate.py`
   before deploying.

## Known limitations / what I'd change with more time

- Lambda's 15-minute execution limit means this only works for
  incremental/delta migrations, not a full historical dump — a large
  initial migration would need to run on Fargate instead. I scoped
  Lambda first because most of the volume was expected to be ongoing
  sync, not a one-time bulk move.
- The Okta custom attributes (`boxUserId`, `googleWorkspaceId`) need to be
  populated during provisioning for this to work cleanly. Backfilling them
  for existing users was the actual bottleneck when I scoped this at
  Invitae — worth automating with an Okta Workflow if I revisit it.
