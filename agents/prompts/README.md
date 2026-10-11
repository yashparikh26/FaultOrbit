# Agent prompts

One Markdown file per prompt, loaded with `agents.prompting.load_prompt(name)`.

- Keep facts and numbers out of prompts: the code computes them and passes them in.
- Placeholders use `{name}`; write literal braces as `{{` and `}}`.
- Every call logs a hash of its system prompt (`prompt_version` in `results/llm_calls.jsonl`).
