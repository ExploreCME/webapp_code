"""Generate PANCE-style questions from the console and write them to a text file.

Uses the same generation/verification pipeline as the web app
(``Question_writing_and_verification_optomized.process_topic_batch``) but never
touches the database.

Each request is "Organ System: Topic: Task Area", e.g.

    python generate_questions_to_file.py \
        "Cardiovascular system: stable angina pectoris: using diagnostics and laboratory studies" \
        "Dermatology: erythema nodosum: formulating the most likely diagnosis"

    python generate_questions_to_file.py --input requests.txt --count 2 --output questions.txt

Request files contain one request per line; blank lines and lines starting with
"#" are ignored. Requires OPENAI_API_KEY (or MY_API_KEY) in the environment or .env.
"""

import argparse
import asyncio
import sys
from datetime import datetime

from Question_writing_and_verification_optomized import ConversationManager, process_topic_batch
from standardizers import TASK_AREAS, standardize_organ_system, standardize_task_area

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_CONCURRENCY = 5


def parse_request(text):
    """Parse "Organ: Topic: Task" into a request dict with canonical categories."""
    parts = [part.strip() for part in text.strip().rstrip(".").split(":")]
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"Expected 'Organ System: Topic: Task Area', got {text!r}")
    raw_organ, topic, raw_task = parts
    organ = standardize_organ_system(raw_organ, default=None)
    if organ is None:
        raise ValueError(f"Unrecognized organ system {raw_organ!r} in {text!r}")
    task = standardize_task_area(raw_task)
    if task not in TASK_AREAS:
        raise ValueError(f"Unrecognized task area {raw_task!r} in {text!r}")
    return {"organ_system": organ, "topic_area": topic, "task_area": task}


def load_requests(args):
    lines = list(args.requests)
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            lines.extend(line for line in f if line.strip() and not line.lstrip().startswith("#"))
    return [parse_request(line) for line in lines]


def format_question(number, q):
    lines = [
        f"Question {number}",
        f"Organ System: {q['organ_system']}",
        f"Topic: {q['topic_area']}",
        f"Task Area: {q['task_area']}",
        f"Difficulty: {q.get('difficulty_rating')}",
        "",
        str(q["question_stem"]),
        "",
        *(f"{letter}. {q[f'answer_choice_{letter}']}" for letter in "ABCDE"),
        "",
        f"Correct Answer: {q['correct_answer']}",
        f"Explanation: {q['explanation']}",
        "",
        "-" * 80,
        "",
    ]
    return "\n".join(lines) + "\n"


def write_questions(path, questions):
    with open(path, "w", encoding="utf-8") as f:
        f.write("--- GENERATED QUESTIONS ---\n")
        f.write(f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"Total Questions: {len(questions)}\n")
        f.write("=" * 80 + "\n\n")
        for number, q in enumerate(questions, 1):
            f.write(format_question(number, q))


async def generate(requests, count, model, concurrency):
    conv_manager = ConversationManager(model=model)
    conv_manager_verify = ConversationManager(model=model)
    semaphore = asyncio.Semaphore(concurrency)

    async def run(request):
        async with semaphore:
            stats = {}
            try:
                return await process_topic_batch(dict(request), count, conv_manager, conv_manager_verify, stats)
            except Exception as error:
                print(f"ERROR: [{request['organ_system']} / {request['topic_area']}] {type(error).__name__}: {error}")
                return []

    try:
        # Results stay in request order regardless of completion order.
        return await asyncio.gather(*(run(request) for request in requests))
    finally:
        await conv_manager.client.close()
        await conv_manager_verify.client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate verified PANCE questions to a text file (no database).")
    parser.add_argument("requests", nargs="*", help='"Organ System: Topic: Task Area" (repeatable)')
    parser.add_argument("-i", "--input", help="File with one request per line")
    parser.add_argument("-n", "--count", type=int, default=1, help="Questions per request (default: 1)")
    parser.add_argument("-o", "--output", help="Output text file (default: generated_questions_<timestamp>.txt)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model (default: {DEFAULT_MODEL})")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Parallel requests")
    args = parser.parse_args(argv)

    if args.count < 1 or args.concurrency < 1:
        parser.error("--count and --concurrency must be at least 1")
    try:
        requests = load_requests(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not requests:
        parser.error("Provide at least one request or --input file")

    output = args.output or f"generated_questions_{datetime.now():%Y%m%d_%H%M%S}.txt"
    for request in requests:
        print(f"Request: {request['organ_system']} | {request['topic_area']} | {request['task_area']}")

    results = asyncio.run(generate(requests, args.count, args.model, args.concurrency))
    questions = [q for batch in results for q in batch]
    write_questions(output, questions)

    shortfalls = [(r, len(batch)) for r, batch in zip(requests, results) if len(batch) < args.count]
    for request, made in shortfalls:
        print(f"WARNING: {request['topic_area']} produced {made}/{args.count} verified questions.")
    print(f"Wrote {len(questions)}/{len(requests) * args.count} questions to '{output}'.")
    return 1 if shortfalls else 0


if __name__ == "__main__":
    sys.exit(main())
