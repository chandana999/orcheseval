from prometheus_client import Counter, Gauge, Histogram, generate_latest

PAYLOADS_INGESTED = Counter(
    "eval_platform_payloads_ingested_total",
    "Agent execution payloads persisted to PostgreSQL",
    ["source_type"],
)
JOBS_CREATED = Counter(
    "eval_platform_jobs_created_total",
    "Evaluation jobs created",
)
JOBS_TERMINAL = Counter(
    "eval_platform_jobs_terminal_total",
    "Evaluation jobs reaching a terminal status",
    ["status"],
)
TICKETS_CREATED = Counter(
    "eval_platform_tickets_created_total",
    "Evaluation tickets created",
)
TICKETS_CLAIMED = Counter(
    "eval_platform_tickets_claimed_total",
    "Evaluation tickets claimed by runners",
)
TICKET_TRANSITIONS = Counter(
    "eval_platform_ticket_transitions_total",
    "Ticket status transitions",
    ["from_status", "to_status"],
)
TICKETS_BY_STATUS = Gauge(
    "eval_platform_tickets",
    "Evaluation tickets by status",
    ["status"],
)
TICKETS_RECOVERED = Counter(
    "eval_platform_tickets_recovered_total",
    "RUNNING tickets recovered after lease expiry",
    ["outcome"],
)
EVALUATION_DURATION = Histogram(
    "eval_platform_evaluation_duration_seconds",
    "Evaluator execution time",
    ["evaluator_type"],
    buckets=(0.005, 0.025, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)
EVALUATIONS_TOTAL = Counter(
    "eval_platform_evaluations_total",
    "Evaluator executions by outcome",
    ["evaluator_type", "status"],
)
LLM_TOKENS = Counter(
    "eval_platform_llm_tokens_total",
    "LLM tokens consumed by judge evaluators",
    ["direction"],
)
LLM_COST_USD = Counter(
    "eval_platform_llm_estimated_cost_usd_total",
    "Estimated LLM spend in USD",
)


def metrics_response() -> bytes:
    return generate_latest()
