"""Interactively verify a user-written question and write every result to a text file.

Prompts for a question stem, five answer choices (A-E), and the number of
verification attempts. Each attempt asks the AI model (same model, system
message, and verification prompt as the web app's question pipeline in
``Question_writing_and_verification_optomized``) to pick the correct answer and
explain it. Attempts are independent experiments: each uses a fresh client and a
single stateless request with no shared conversation history.

    python verify_question_to_file.py
    python verify_question_to_file.py --output my_question.txt --model gpt-6.1-sol

Requires OPENAI_API_KEY (or MY_API_KEY) in the environment or .env. The model
defaults to OPENAI_MODEL, then the pipeline's DEFAULT_MODEL.
"""

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime

from Question_writing_and_verification_optomized import (
    DEFAULT_MODEL,
    MAX_UNUSABLE_VERIFIER_RESPONSES,
    PANCE_Verification_CONSTRAINTS,
    PANCE_Verification_SYSTEM,
    PANCE_Verification_TASK,
    ConversationManager,
    extract_correct_letter,
    parse_json_object,
    parse_score,
)

LETTERS = "ABCDE"
MAX_TOKENS = 10000
DEFAULT_CONCURRENCY = 5

VERIFY_WITH_EXPLANATION_OUTPUT = """Output your response as valid JSON with this structure:
{
  "correct_answer": "<single letter A-E>",
  "confidence_score": <integer from 1 to 10, where 10 means completely certain>,
  "explanation": "<explanation of why the answer is correct and why the other choices are incorrect>"
}
Respond with JSON only. No markdown.
"""


def build_prompt(question):
    choices = "\n".join(f"{letter}: {question[f'answer_choice_{letter}']}" for letter in LETTERS)
    return f"""{PANCE_Verification_SYSTEM}
{PANCE_Verification_TASK}
{question['question_stem']}
{choices}
{PANCE_Verification_CONSTRAINTS}
{VERIFY_WITH_EXPLANATION_OUTPUT}
""".strip()


def _read_required(prompt, input_fn):
    while True:
        value = input_fn(prompt).strip()
        if value:
            return value
        print("A value is required.")


def prompt_question(input_fn=input):
    question = {"question_stem": _read_required("Question stem: ", input_fn)}
    for letter in LETTERS:
        question[f"answer_choice_{letter}"] = _read_required(f"Answer choice {letter}: ", input_fn)
    return question


def prompt_attempts(input_fn=input):
    while True:
        raw = input_fn("Number of times to verify the question: ").strip()
        if raw.isdigit() and int(raw) >= 1:
            return int(raw)
        print("Enter a whole number of 1 or more.")


async def run_attempt(number, question, api_key, model):
    """Run one independent experiment and return a result dict."""
    manager = ConversationManager(api_key=api_key, model=model)
    prompt = build_prompt(question)
    result = {"attempt": number, "answer": None, "confidence": None, "explanation": None,
              "errors": [], "raw_response": None}
    try:
        for _ in range(MAX_UNUSABLE_VERIFIER_RESPONSES + 1):
            try:
                response = await manager.chat_completion(prompt, max_tokens=MAX_TOKENS)
            except Exception as error:
                result["errors"].append(f"api_error: {type(error).__name__}: {error}")
                continue
            result["raw_response"] = response
            data, reason = parse_json_object(response)
            if reason:
                result["errors"].append(reason)
                continue
            answer = extract_correct_letter(data.get("correct_answer"), question)
            if not answer:
                result["errors"].append(f"answer_unparseable: {data.get('correct_answer')!r}")
                continue
            result.update(
                answer=answer,
                confidence=parse_score(data.get("confidence_score")),
                explanation=str(data.get("explanation") or "").strip() or "(no explanation provided)",
            )
            return result
        return result
    finally:
        await manager.client.close()


async def run_attempts(question, attempts, api_key, model, concurrency):
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(number):
        async with semaphore:
            result = await run_attempt(number, question, api_key, model)
            status = result["answer"] or "FAILED"
            print(f"Attempt {number}/{attempts}: {status}")
            return result

    return await asyncio.gather(*(bounded(n) for n in range(1, attempts + 1)))


def format_report(question, results, model):
    tally = Counter(r["answer"] for r in results if r["answer"])
    failed = sum(1 for r in results if not r["answer"])
    lines = [
        "--- QUESTION VERIFICATION ---",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Model: {model}",
        f"Attempts: {len(results)}",
        "=" * 80,
        "",
        "Question Stem:",
        question["question_stem"],
        "",
        *(f"{letter}. {question[f'answer_choice_{letter}']}" for letter in LETTERS),
        "",
        "=" * 80,
        "SUMMARY",
        *(f"{letter}. selected {tally[letter]}/{len(results)} times" for letter in LETTERS),
        f"Failed attempts: {failed}/{len(results)}",
    ]
    if tally:
        top = max(tally.values())
        leaders = [letter for letter in LETTERS if tally[letter] == top]
        lines.append(f"Most frequent answer: {', '.join(leaders)} ({top}/{len(results)})")
    lines += ["=" * 80, ""]

    for r in results:
        lines.append(f"Attempt {r['attempt']}")
        if r["answer"]:
            confidence = "unparseable" if r["confidence"] is None else f"{r['confidence']:g}/10"
            choice_text = question["answer_choice_" + r["answer"]]
            lines += [
                f"Correct Answer: {r['answer']}. {choice_text}",
                f"Confidence: {confidence}",
                f"Explanation: {r['explanation']}",
            ]
        else:
            lines.append("Correct Answer: FAILED (no usable response)")
            if r["raw_response"]:
                lines.append(f"Last raw response: {r['raw_response']}")
        if r["errors"]:
            lines.append(f"Retried/unusable responses: {'; '.join(r['errors'])}")
        lines += ["", "-" * 80, ""]
    return "\n".join(lines)


def main(argv=None, input_fn=input):
    parser = argparse.ArgumentParser(
        description="Ask the AI model to answer and explain a question multiple times (independently).")
    parser.add_argument("-o", "--output", help="Output text file (default: question_verification_<timestamp>.txt)")
    parser.add_argument("--model", help=f"OpenAI model (default: OPENAI_MODEL or {DEFAULT_MODEL})")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Parallel attempts")
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    api_key = next(
        (value.strip() for value in (os.getenv("OPENAI_API_KEY"), os.getenv("MY_API_KEY"))
         if value and value.strip()),
        None,
    )
    if not api_key:
        parser.error("Set OPENAI_API_KEY or MY_API_KEY in the environment or .env file.")
    model = args.model or os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_MODEL

    question = prompt_question(input_fn)
    attempts = prompt_attempts(input_fn)
    output = args.output or f"question_verification_{datetime.now():%Y%m%d_%H%M%S}.txt"

    print(f"Running {attempts} independent attempt(s) with model {model}...")
    results = asyncio.run(run_attempts(question, attempts, api_key, model, args.concurrency))
    with open(output, "w", encoding="utf-8") as f:
        f.write(format_report(question, results, model))

    failed = sum(1 for r in results if not r["answer"])
    print(f"Wrote {attempts} attempt(s) to '{output}'" + (f" ({failed} failed)." if failed else "."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
