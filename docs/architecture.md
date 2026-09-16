# KapConnect architecture

This document explains how KapConnect is put together and why. The README gives the short version.

## Contents

1. [Context](#context)
2. [Component view](#component-view)
3. [End to end result flow](#end-to-end-result-flow)
4. [Regional outage and recovery](#regional-outage-and-recovery)
5. [Reagent recall](#reagent-recall)
6. [Tamper detection and repair](#tamper-detection-and-repair)
7. [Deployment view](#deployment-view)
8. [Data model](#data-model)
9. [Security model](#security-model)
10. [Scaling path](#scaling-path)

## Context

Kappa Diagnostics (KapD) sells analysers such as the Sigma immunoassay unit. Hospitals connect those analysers to many different laboratory and hospital systems, often by hand. KapConnect is the digital services layer that sits between the analysers and everything downstream, and it has to keep working when a cloud region fails.

```mermaid
flowchart LR
    A[KapD analysers<br/>Sigma, Chem, Haem] -->|HL7 v2 ORU| K[KapConnect]
    K -->|FHIR R4| E[Hospital EMR]
    K -->|Push alerts| C[Clinicians]
    K --> L[Lab technicians]
    K --> H[Hospital administrators]
    K --> O[IT operations]
    K <-->|S3, SQS, KMS| AWS[(AWS Sydney)]
    K <-->|Blob, Service Bus, Key Vault| AZ[(Azure Australia East)]
```

## Component view

```mermaid
flowchart TB
    subgraph Edge["Hospital edge node"]
        API[FastAPI ingestion<br/>HL7 v2 parser]
        SCR[AI screening<br/>RandomForest plus guardrails]
        UPI[Universal Patient Index<br/>Fellegi Sunter, encrypted]
        RPR[Provenance ledger<br/>SHA 256 chain, HMAC]
        ALR[Critical value alerts<br/>15 minute escalation]
        RT[Multi cloud router<br/>breakers, failover, cost routing]
        OB[(Edge buffer<br/>transactional outbox)]
        DB[(Operational store<br/>SQLite)]
        W[Background worker<br/>consume, probe, replay, escalate]
    end

    subgraph AWS["AWS ap-southeast-2"]
        S3[(S3 Object Lock<br/>COMPLIANCE)]
        SQS[[SQS plus DLQ]]
        KMS{{KMS CMK}}
    end

    subgraph AZ["Azure australiaeast"]
        BLOB[(Blob with WORM policy)]
        SB[[Service Bus plus DLQ]]
        KV{{Key Vault key}}
    end

    API --> SCR --> UPI --> RPR --> RT
    API --> ALR
    RT --> S3 & BLOB
    RT --> SQS & SB
    RT -. all clouds down .-> OB
    UPI --> KMS & KV
    W --> RT
    API --> DB
```

| Component | Code | Responsibility |
| --- | --- | --- |
| Cloud abstraction | `app/cloud/base.py` | Three provider neutral interfaces: object store, message queue, key provider |
| AWS adapter | `app/cloud/aws.py` | S3 Object Lock, SQS with redrive policy, KMS data keys |
| Azure adapter | `app/cloud/azure.py` | Blob Storage, Storage Queue or Service Bus, Key Vault wrap and unwrap |
| Router | `app/cloud/router.py` | Circuit breakers, failover, dual writes, edge buffer, outage drills |
| FinOps | `app/cloud/finops.py` | Meters every call by provider, operation and site |
| Interoperability | `app/integration/` | HL7 v2 parsing and ACKs, FHIR R4 transaction bundles |
| Screening | `app/screening/model.py` | Pre analytical error detection |
| Patient index | `app/mpi/matcher.py` | Probabilistic matching, blind index lookup, review queue |
| Provenance | `app/provenance/ledger.py` | Hash chain, replication, verification, repair |
| Governance | `app/governance/policy.py` | Runtime compliance posture |
| Orchestration | `app/platform.py` | Wires the flow together |

## End to end result flow

```mermaid
sequenceDiagram
    autonumber
    participant D as Sigma analyser
    participant K as KapConnect edge
    participant M as Patient index
    participant S as AI screening
    participant L as Ledger
    participant A as AWS
    participant Z as Azure
    participant Q as Event consumer
    participant C as Clinician

    D->>K: ORU^R01 with ZRP and ZSQ segments
    K->>K: Validate, reject with MSA AR if invalid
    K->>K: Idempotency check on MSH-10
    K->>M: Resolve patient (MRN, then probabilistic)
    M-->>K: existing, new, possible or probable
    K->>S: Screen sample quality
    S-->>K: label, confidence, hold decision
    K->>L: Append RPR (hash, sign)
    L->>A: PutObject with Object Lock
    L->>Z: Upload blob (no overwrite)
    K->>A: Publish result.ready to SQS
    Note over K,A: Falls to Azure, then edge buffer, if unavailable
    K-->>D: MSA AA
    Q->>A: Receive message
    Q->>Q: Skip if message id already processed
    Q->>C: Deliver to portal and EMR
    alt Critical value and released
        K->>C: Alert
        Note over K,C: Escalates to on call after 15 minutes
    end
```

Held samples do not raise critical value alerts. A haemolysed sample often shows a falsely high potassium, so the safe action is recollection rather than an emergency page.

## Regional outage and recovery

```mermaid
sequenceDiagram
    autonumber
    participant K as Router
    participant A as AWS
    participant Z as Azure
    participant B as Edge buffer

    K->>A: publish
    A--xK: timeout
    K->>K: breaker failure 1 of 3
    K->>Z: publish (failover)
    Z-->>K: ok
    Note over K: After 3 failures the AWS breaker opens<br/>and AWS is skipped without waiting
    K->>Z: publish
    Z--xK: timeout
    K->>B: store operation locally
    Note over K,B: Result is still accepted and acknowledged
    loop every second
        K->>A: health probe once cooldown ends
    end
    A-->>K: healthy, breaker closes
    K->>B: read buffered operations
    K->>A: replay publish and archive
    K->>K: mark ledger replica confirmed
```

Ledger writes are replicated to every cloud, so a result written during an AWS outage has an Azure copy immediately and an AWS copy once AWS returns. The ledger row records the state of each replica (`pending` or its location), which is what `/api/v1/ledger/verify` reports.

## Reagent recall

The recall query uses the reagent batch recorded in each result's provenance. Results are joined to the golden patient identity, so a patient seen at two hospitals is counted once after an administrator links the records.

```mermaid
sequenceDiagram
    participant T as Lab technician
    participant K as KapConnect
    participant DB as Operational store
    T->>K: GET /api/v1/recall/LOT-S24A117
    K->>DB: results by reagent_batch (indexed) joined to golden patient
    DB-->>K: affected results
    K-->>T: patients, results already delivered, analysers, elapsed ms
    K->>DB: audit log entry
```

In a local benchmark of 3,000 results, a recall touching 300 results and 108 patients returned in under 4 ms.

## Tamper detection and repair

```mermaid
flowchart LR
    X[Insider edits a value<br/>in the database] --> V{Verify chain}
    V -->|hash mismatch at entry n| R[Repair entry n]
    R --> W1[(AWS locked copy)]
    R -.if AWS unavailable.-> W2[(Azure locked copy)]
    W1 --> OK[Entry restored<br/>chain valid]
    W2 --> OK
```

Each entry hash covers the previous hash and the canonical JSON of the entry, and the hash is signed with an HMAC key derived from the data key. Changing any field breaks the chain at that entry. The cloud copies cannot be edited or deleted before their retention date, so they are the source of truth for repair.

## Deployment view

```mermaid
flowchart TB
    subgraph GH[GitHub]
        CI[CI: ruff, pytest, moto, Azurite,<br/>docker build, Trivy, terraform validate]
        CD[Deploy workflow<br/>OIDC federation]
    end

    subgraph AWSD[AWS account]
        ALB[Application Load Balancer] --> ECS[ECS Fargate service]
        ECS --> S3B[(S3 Object Lock bucket)]
        ECS --> SQSQ[[SQS and DLQ]]
        ECS --> KMSK{{KMS key}}
        ECS --> SM[Secrets Manager]
        ECS --> CW[CloudWatch logs and alarms]
        BUD[AWS Budgets]
        ECR[(ECR)] --> ECS
    end

    subgraph AZD[Azure subscription]
        ACA[Container Apps] --> STG[(Storage account<br/>WORM container)]
        ACA --> SBN[[Service Bus queue]]
        ACA --> KVT{{Key Vault}}
        MI[User assigned managed identity] --- ACA
        LAW[Log Analytics] --- ACA
        POL[Azure Policy<br/>allowed locations]
        CB[Consumption budget]
    end

    CD --> ECR
    CD --> GHCR[(GHCR)] --> ACA
```

Terraform lives in `infra/terraform/aws` and `infra/terraform/azure`. Both modules refuse regions outside Australia at plan time.

## Data model

| Table | Purpose |
| --- | --- |
| `patients` | Encrypted demographics (AES 256 GCM) and the golden identity |
| `patient_index` | HMAC blind index tokens used for candidate lookup |
| `match_reviews` | Possible and probable matches waiting for a decision |
| `results` | One row per observation with screening, provenance fields and FHIR bundle |
| `ledger` | Hash chained provenance entries and replica state |
| `alerts` | Critical values and their acknowledgement or escalation |
| `outbox` | Operations held at the edge while clouds are unavailable |
| `processed_messages` | Idempotency keys for the event consumer |
| `deliveries` | Where each released result was delivered |
| `keyring` | The data key wrapped by each cloud's key service |
| `audit_log` | Who did what and when |
| `cost_events` | Metered cloud usage |

## Security model

* **Identity.** Demo users sign in with short lived JWTs. Production would federate with Amazon Cognito or Microsoft Entra ID; `app/core/security.py` is the only module that changes.
* **Authorisation.** Every route declares a permission, for example `cloud:operate` or `reviews:decide`, and roles map to permission sets.
* **Data minimisation.** Roles without `phi:read` (IT operations, hospital administrators) receive masked names, dates of birth and identifiers, and FHIR bundles are redacted for them. The one exception is the match review queue, where administrators need full demographics to decide; every view of it is audited.
* **Encryption.** Patient demographics use envelope encryption. The data key is wrapped by AWS KMS and by Azure Key Vault, so either cloud can unlock the data.
* **No stored cloud keys.** Workloads use ECS task roles and Azure managed identity. Azure Storage account keys and Service Bus SAS keys are disabled in Terraform. GitHub deployments use OIDC.
* **Transport.** S3 denies non TLS requests, Azure enforces TLS 1.2, and the ALB supports TLS 1.3 when a certificate is supplied.
* **Audit.** Logins, ingestion outcomes, outage drills, recalls, match decisions and ledger operations are written to `audit_log`.

## Scaling path

The current build favours clarity and a zero setup demo. These are the next steps for production scale, in order:

1. Move the operational store from SQLite to Amazon Aurora PostgreSQL or Azure Database for PostgreSQL. All SQL sits in `app/storage/db.py` and the modules that call it, so the change is contained. This also removes the single replica limit set in Terraform.
2. Split the background worker into its own service so consumers scale separately from ingestion.
3. Replace polling consumers with Amazon MSK or Azure Event Hubs when volume moves past what SQS and Service Bus handle comfortably.
4. Send FHIR bundles to AWS HealthLake or Azure Health Data Services.
5. Retrain the screening model on real laboratory data with clinical governance.
