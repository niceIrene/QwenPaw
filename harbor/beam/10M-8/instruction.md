# BEAM long-term-memory evaluation

Use QwenPaw to answer every probe in `/app/questions.json` from the historical
conversation in `/app/chat.json`.

Import the conversation into QwenPaw's durable Scroll history once. Run each
probe in a fresh session and use QwenPaw's history-recall tool to recover the
relevant evidence. Do not let one probe's answer become context for another.

Write `/app/answers.json` as an object grouped by question `type`. Each entry
must contain the original `id`, the original `question`, and `llm_response`:

```json
{
  "abstention": [
    {
      "id": "abstention-0",
      "question": "...",
      "llm_response": "..."
    }
  ]
}
```

Answer only from recalled conversation evidence and obey each probe's count or
formatting constraints. Never inspect `/tests` or `/solution`.

