Absolutely. I can create a **clean documentation section based specifically on the article you provided**, explaining the **Offline Baseline → Online Guardrail** model in a way you can use in your Eval Platform documentation.

# Offline Baseline to Online Evaluation Model

## 1. Purpose

The evaluation platform uses two complementary evaluation stages:

1. **Offline Evaluation** — evaluates a candidate agent/model/prompt against a frozen evaluation dataset before deployment.
2. **Online Evaluation** — evaluates the deployed application using sampled production traffic after deployment.

The key principle is that **offline evaluation establishes the approved quality baseline**, while online evaluation monitors production behavior against that baseline.

This creates a continuous evaluation loop:

```text
Offline Evaluation
        ↓
Approved Baseline
        ↓
Deployment
        ↓
Online Evaluation
        ↓
Production Monitoring
        ↓
Detect Regression / Drift
        ↓
Feed Failed Cases Back
        ↓
Next Offline Evaluation
```

The source article describes offline evaluation as the pre-deployment gate and online evaluation as the mechanism for measuring real production behavior. 

---

# 2. Offline Evaluation

Offline evaluation runs before deployment against a **frozen evaluation set**.

The candidate version is evaluated against known cases, using deterministic metrics, judge-based metrics, or other configured evaluators.

The purpose is to determine whether the candidate introduces a regression before it reaches production.

```text
Frozen Evaluation Dataset
          ↓
Candidate Version
          ↓
Run Evaluators
          ↓
Calculate Metrics
          ↓
Compare with Existing Baseline
          ↓
Offline Gate
```

The source describes offline evaluation as a pre-deployment gate against a frozen set and states that its primary purpose is to block regressions.  

---

# 3. Establishing the Offline Baseline

Once an evaluation version has passed the offline gate, its approved metric becomes the **baseline for production monitoring**.

For example:

```text
Metric: Faithfulness

Previous approved baseline = 0.91

Candidate offline result = 0.92

Offline evaluation:
0.92 vs 0.91

Result:
PASS
```

The article provides this exact illustrative example:

```text
faithfulness 0.92 vs 0.91 baseline
verdict PASS
```

and states that the gate opens and deployment proceeds. 

The baseline therefore represents the quality level that was approved before deployment.

---

# 4. Deployment

After the candidate passes the offline gate, the version can be deployed.

```text
Offline Score
     ↓
Compare Against Baseline
     ↓
PASS
     ↓
Deploy
```

Importantly, a successful offline evaluation is **not treated as a guarantee that production quality will remain unchanged**.

The source explains that the offline dataset represents a fixed distribution, while production traffic can change after deployment. 

---

# 5. Online Evaluation

After deployment, the system evaluates a sample of real production traffic.

The online evaluator can use the same evaluation logic used during offline evaluation.

```text
Production Request
        ↓
OpenTelemetry / Application Trace
        ↓
Sampling
        ↓
Online Evaluator
        ↓
Metric Result
        ↓
Compare Against Baseline / Guardrail
```

The source explicitly describes online evaluation as scoring sampled production requests and triggering a guardrail when a metric crosses a threshold. 

The article also recommends using the **same evaluators** for production sampling as those used by the offline gate. 

---

# 6. How the Offline Baseline Is Used Online

The important relationship is:

```text
Offline Approved Baseline
            ↓
       Online Monitor
            ↓
Production Metric
            ↓
Compare
            ↓
Guardrail Decision
```

For example:

```text
Offline approved baseline = 0.91

Online allowed degradation = 0.03

Online guardrail = 0.88
```

Therefore:

```text
Online score >= 0.88
        → Healthy

Online score < 0.88
        → Regression / Guardrail Trigger
```

The article's illustrative example demonstrates this pattern using:

```text
Offline baseline = 0.91
Online result    = 0.84
Guardrail budget = 0.03
```

The resulting online guardrail is triggered because the production result has degraded beyond the permitted budget. 

### Important terminology

The platform should distinguish:

| Concept                     | Meaning                                                       |
| --------------------------- | ------------------------------------------------------------- |
| **Offline Baseline**        | Approved quality level established before deployment          |
| **Offline Candidate Score** | Score produced by the candidate during offline evaluation     |
| **Online Score**            | Metric measured from sampled production traffic               |
| **Online Guardrail Budget** | Maximum permitted degradation from the approved baseline      |
| **Online Guardrail**        | Minimum acceptable production score derived from the baseline |
| **Regression**              | Production quality falling beyond the allowed degradation     |

This distinction avoids treating the offline and online thresholds as two unrelated values.

---

# 7. Example End-to-End Flow

Consider a `faithfulness` evaluator.

### Step 1 — Offline

```text
Previous baseline = 0.91
Candidate score   = 0.92
```

The candidate passes the offline gate.

```text
0.92 ≥ required offline quality
        ↓
PASS
```

The approved baseline is retained:

```text
approved_baseline = 0.91
```

---

### Step 2 — Define Online Guardrail

Assume the organization allows a maximum degradation of `0.03`.

```text
Online Guardrail
    = Offline Baseline - Allowed Degradation

    = 0.91 - 0.03

    = 0.88
```

---

### Step 3 — Production

Production traffic is sampled.

```text
Production requests
        ↓
Online evaluation
        ↓
faithfulness = 0.84
```

---

### Step 4 — Compare

```text
Baseline       = 0.91
Online score   = 0.84
Degradation    = 0.07

Allowed        = 0.03
```

Therefore:

```text
0.07 > 0.03

REGRESSION
```

The article illustrates this with the same numbers and describes the resulting action as a guardrail trip. 

---

# 8. Online Regression Handling

When an online guardrail is triggered, the platform should preserve the production evidence that caused the regression.

```text
Online Regression
        ↓
Identify Failing Production Traces
        ↓
Capture Evaluation Context
        ↓
Label / Review Failure
        ↓
Add Representative Cases
        ↓
Next Frozen Evaluation Dataset
```

The source specifically describes pulling failing production traces, labeling them, and adding them to the next frozen evaluation set. 

This creates a feedback loop:

```text
                 ┌─────────────────────┐
                 │   Offline Dataset   │
                 └──────────┬──────────┘
                            │
                            ▼
                    Offline Evaluation
                            │
                            ▼
                     Approved Baseline
                            │
                            ▼
                         Deploy
                            │
                            ▼
                    Online Evaluation
                            │
                            ▼
                   Production Failure
                            │
                            ▼
                    Capture Trace
                            │
                            ▼
                 Add to Offline Dataset
                            │
                            └──────────────►
```

The article describes offline and online evaluation as **two stages of one feedback loop**, rather than two disconnected evaluation systems. 

---

# 9. Why the Same Evaluator Should Be Used

For the comparison to be meaningful, the online evaluation should use the same evaluator definition where possible.

For example:

```text
Offline:
Faithfulness Evaluator v1

Online:
Faithfulness Evaluator v1
```

This allows the platform to compare:

```text
Offline baseline
        ↕
Online production metric
```

rather than comparing two different measurement systems.

The source explicitly recommends sampling production traffic and scoring it with the **same evaluators** used by the offline gate. 

---

# 10. Offline and Online Responsibilities

| Area            | Offline Evaluation                | Online Evaluation                |
| --------------- | --------------------------------- | -------------------------------- |
| Execution       | Before deployment                 | After deployment                 |
| Data            | Frozen evaluation set             | Sampled production traffic       |
| Ground truth    | Known/reference cases             | Usually no direct ground truth   |
| Primary purpose | Prevent regressions               | Detect production drift/failures |
| Baseline        | Establishes approved baseline     | Uses baseline for comparison     |
| Decision        | Deploy / block                    | Healthy / alert / investigate    |
| Environment     | CI / evaluation environment       | Production                       |
| Feedback        | Creates approved release baseline | Produces new failure cases       |
| Dataset         | Frozen                            | Continuously sampled             |
| Evaluators      | Configured evaluators             | Same evaluators where applicable |

The article makes the same distinction between offline and online evaluation: offline gates the deployment, while online evaluates the live distribution and detects drift and failures. 

---

# 11. Recommended Eval Platform Model

For an evaluation platform, the relationship can be represented as:

```text
                    EVALUATION PLATFORM
                           │
             ┌─────────────┴─────────────┐
             │                           │
             ▼                           ▼
      OFFLINE EVALUATION          ONLINE EVALUATION
             │                           │
       Frozen Dataset              Production Traces
             │                           │
             ▼                           ▼
       Run Evaluators             Run Same Evaluators
             │                           │
             ▼                           ▼
       Metric Results             Metric Results
             │                           │
             ▼                           │
      Approved Baseline ────────────────┘
             │
             ▼
      Online Guardrail
             │
             ▼
      Production Decision
             │
       ┌─────┴─────┐
       ▼           ▼
    Healthy      Regression
                     │
                     ▼
               Failed Traces
                     │
                     ▼
              Offline Dataset
```

---

# 12. Key Design Principle

The platform should therefore implement the following relationship:

```text
Offline Evaluation
        ↓
Approved Baseline
        ↓
Online Guardrail Configuration
        ↓
Production Evaluation
        ↓
Compare Online Result
        ↓
Detect Regression
```

The **baseline is the reference point**, while the **online guardrail defines how much deviation from that baseline is acceptable**.

This is preferable to creating an unrelated online threshold because the production monitor is anchored to a quality level that was already evaluated and approved before deployment.

---

# 13. Continuous Improvement Loop

The final lifecycle is:

```text
DISCOVER
   ↓
Build Evaluation Dataset
   ↓
OFFLINE EVALUATION
   ↓
Establish / Update Baseline
   ↓
Release Gate
   ↓
DEPLOY
   ↓
ONLINE EVALUATION
   ↓
Monitor Production Quality
   ↓
Guardrail / Regression Detection
   ↓
Capture Production Failures
   ↓
Enrich Evaluation Dataset
   ↓
NEXT OFFLINE EVALUATION
   ↓
New Baseline
```

This approach keeps the offline evaluation suite evolving based on real production failures while retaining a controlled, frozen dataset for each evaluation version.

The source explicitly recommends this direction: **offline gates the release, online watches the release, and new production cases are fed back into the next frozen offline set.** 

If you want, I can also turn this into a **1-page architecture/design document specifically for your `eval-platform`**, with the **DB fields (`baseline_score`, `online_guardrail`, `degradation_budget`, etc.), evaluation lifecycle, and API flow**.
