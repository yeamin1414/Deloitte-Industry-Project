# Five minute demo guide

A script for showing KapConnect in an interview or screen share, followed by the questions engineers usually ask.

## Before the call

```bash
docker compose up --build        # or: make install && make run
```

Open http://localhost:8000 and click **Send 25 device results** twice so the console has data.

## The walkthrough

**1. The problem (30 seconds).** Hospitals run analysers that do not talk to each other, results get retyped, and when the network drops, results stop. KapConnect is the integration layer that keeps results flowing, provable and affordable across AWS and Azure.

**2. The happy path (45 seconds).** Point at the route map. Results arrive as HL7 v2 messages, are screened by the AI model, signed into the ledger, copied to locked storage in both clouds and routed through AWS Sydney to clinicians. Switch the role to **Dr Anna Jones** and show real names; switch to **Minh Nguyen** and show masked names. Same data, different access.

**3. Take AWS offline (60 seconds).** Click **Take AWS Sydney offline**, then **Send 25 device results**. Traffic moves to Azure with no errors. Point at the edge node: ledger copies meant for AWS are held locally.

**4. Take Azure offline too (45 seconds).** Both clouds are down. Send results again. The hospital keeps accepting and acknowledging results and the held count rises. Bring AWS back and watch the count drain to zero.

**5. Prove the records cannot be faked (45 seconds).** In **Result provenance**, click **Simulate an edited result**, then **Check the chain**. The break is found at the exact entry. Click **Restore from cloud copy**, then check again.

**6. Recall and cost (45 seconds).** Enter `LOT-S24A117` and click **Find affected patients**. Then point at **Cloud cost**: cost per result, spend by cloud, projection at 1.5 million results a month, and the switch to route by lowest cost.

**7. Close (30 seconds).** Everything shown runs in CI against AWS and Azure emulators, and the same code deploys to ECS Fargate and Azure Container Apps with Terraform.

## Questions you should be ready for

**Why build a provider abstraction instead of picking one cloud?**
Hospitals worry about vendor lock in and regional outages, and many already have one cloud mandated. Three small interfaces (object store, queue, key provider) are enough to swap clouds without touching business logic. Failover, dual writes and cost routing all sit on top of that seam.

**How does the circuit breaker work?**
Each provider has a counter. After three consecutive failures the breaker opens and the router skips that provider immediately instead of waiting on timeouts. After a cooldown it becomes half open; the next health probe either closes it or opens it again. See `CircuitBreaker` in `app/cloud/router.py`.

**What happens if both clouds are down?**
Operations are written to an outbox table on the hospital node in the same database as the result, which is the transactional outbox pattern. The worker replays them when a provider recovers. Writes are idempotent, so replaying twice is safe.

**How do you avoid processing a message twice?**
SQS and Service Bus deliver at least once. The consumer records each message id in `processed_messages` inside the same transaction as the delivery, and skips ids it has already seen.

**What is actually immutable?**
S3 objects are written with Object Lock in COMPLIANCE mode, which even the root account cannot shorten. The Azure container has a time based retention policy. The test suite proves S3 rejects deletion of a locked object version.

**Why hash chain the ledger if the cloud copies are already locked?**
The locked copies prove what was written. The chain proves the operational database still matches, and pinpoints which entry changed without comparing every object.

**How accurate is the AI model?**
On a synthetic holdout set: 98.6 percent of bad samples are caught, 2.6 percent of good samples are held, macro F1 is 0.95. The data is synthetic, so these numbers show the pipeline works, not clinical performance. Hard rules for wrong tube and very low volume always override the model.

**Why never merge patient records automatically?**
A wrong merge puts one patient's results in another patient's chart, which is a serious safety event. The matcher scores candidates with the Fellegi Sunter model and sends likely matches to an administrator. Linking keeps both records and points them at one golden identity, so it can be reversed.

**How are patient details protected if the database leaks?**
Demographics are encrypted with AES 256 GCM. The data key is wrapped by AWS KMS and Azure Key Vault. Lookups use HMAC blind index tokens, so the database never holds searchable plaintext.

**Why SQLite?**
It keeps the demo runnable with one command. All SQL is isolated, and the Terraform pins the service to one replica until the store moves to PostgreSQL. That trade off is documented rather than hidden.

**How are costs estimated?**
Every cloud call is recorded with a unit price from `config/pricing.json`. The prices are illustrative and should be refreshed from the AWS Price List API and the Azure Retail Prices API.

**What would you do next?**
Move to PostgreSQL, split the worker into its own service, send FHIR bundles to AWS HealthLake or Azure Health Data Services, and retrain the model on real data.
