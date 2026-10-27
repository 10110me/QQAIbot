# Per-model retries

Each model has a `max_retry` setting (0–20) in **Model settings**. This counts retries after the first request, not total attempts. A new model added through the Web UI defaults to 2 retries. Existing models, or API-created models that omit this field, continue to use their provider's `max_retry` value until they are edited.

Retryable failures wait `1, 2, 4, 8, 16, 16, …` seconds before the next attempt. Non-retryable errors do not wait. When a model returns HTTP 429 and another configured model is available, routing switches to the next model immediately. The conversation's overall deadline can stop retries before the configured count is exhausted. Each API request still occupies a per-endpoint concurrency slot only while the network attempt is in flight, not during its retry wait.
