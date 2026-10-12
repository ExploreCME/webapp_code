####Question_writing_and_verification_optomized.py
import os
import json
import math
from db_pool import get_async_db_connection
import re
import asyncio
import uuid
import random
import traceback
from standardizers import standardize_organ_system, standardize_task_area
from openai import AsyncOpenAI
from datetime import datetime
from dotenv import load_dotenv

# Point exactly to where your .env file lives
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
DEFAULT_OPENAI_API_KEY = os.getenv('OPENAI_API_KEY') or os.getenv('MY_API_KEY')

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_SYSTEM_MESSAGE = "You are an experienced professor in a Physician Assistant program."

class ConversationManager:
    def __init__(self, api_key=None, model=None, max_tokens=None, token_budget=None, system_message=None):
        api_key = api_key or DEFAULT_OPENAI_API_KEY
        if not api_key:
            raise RuntimeError("Set OPENAI_API_KEY or MY_API_KEY in the environment or .env file.")
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model if model is not None else DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.system_message = system_message if system_message is not None else DEFAULT_SYSTEM_MESSAGE

    async def chat_completion(self, prompt, max_tokens=None):
        max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        messages = [
            {"role": "system", "content": self.system_message},
            {"role": "user", "content": prompt},
        ]
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_completion_tokens=max_tokens,
            response_format={ "type": "json_object" }
        )
        return response.choices[0].message.content

    def build_PANCE_batch_prompt(self, organ_system, topic_area, task_area, num_questions, summary=None):
        summary_instruction = f"\n- **CONTEXT**: Ensure the medical facts align with this study summary:\n{summary}\n" if summary else ""

        return f"""You are an experienced professor in a Physician Assistant program.
Create EXACTLY {num_questions} distinct PANCE style exam questions.
Organ System: {organ_system}
Target Diagnosis/Topic: {topic_area}
Task Area: {task_area}
{summary_instruction}

Follow these strict rules to ensure MAXIMUM VARIETY across the {num_questions} questions:
- **CLINICAL VIGNETTES**: Every question MUST have a unique clinical vignette. You MUST vary the patient demographics (age, gender), the clinical setting (ER, outpatient, inpatient), and the presentation style (classic, subtle, acute exacerbation). Do not use the same patient profile twice.
- **SECOND-ORDER QUESTIONING**: Do not just ask "What is the most likely diagnosis?" for every question. Vary the final question sentence. Ask about the pathophysiologic mechanism, the next best diagnostic step, the definitive treatment, associated risk factors, or common complications.
- **ANSWER VARIETY**: Do not make the correct answer the exact same text for every question. Use synonymous medical terms, specific subtypes, or mechanisms.
- **ANTI-AMBIGUITY**: Make distractors plausible but unequivocally incorrect. Do not include conditions that overlap too closely with the correct answer.
- **TARGET DISEASE**: Do NOT name the target diagnosis ({topic_area}) directly in the question stem.
- **OPTIONS**: Provide exactly 5 distinct answer choices for each question.
- **EXPLANATIONS**: Explain why the answer is correct using medical text, NEVER referring to choices by their letters (e.g., never write "A is correct").

- **CATEGORY MAPPING RULE (CRITICAL)**: The `organ_system` and `task_area` in your JSON output MUST EXACTLY MATCH one of the following exact strings. Do not invent your own categories.
  **Allowed Organ Systems**: Cardiovascular System, Dermatologic System, Endocrine System, Eyes, Ears, Nose, and Throat, Gastrointestinal System/Nutrition, Genitourinary System, Hematologic System, Infectious Diseases, Musculoskeletal System, Neurologic System, Psychiatry/Behavioral Science, Pulmonary System, Renal System, Reproductive System.
  **Allowed Task Areas**: History Taking and Performing Physical Examination, Using Diagnostic and Laboratory Studies, Formulating the Most Likely Diagnosis, Health Maintenance, Patient Education, and Preventive Measures, Clinical Intervention, Pharmaceutical Therapeutics, Applying Foundational Scientific Concepts.

Output your response as valid JSON with this exact structure:
{{
  "questions": [
    {{
      "organ_system": "{organ_system}",
      "task_area": "{task_area}",
      "topic_area": "{topic_area}",
      "question_stem": "string",
      "answer_choice_A": "string",
      "answer_choice_B": "string",
      "answer_choice_C": "string",
      "answer_choice_D": "string",
      "answer_choice_E": "string",
      "correct_answer": "string",
      "explanation": "string"
    }}
  ]
}}
Respond with JSON only. No explanations or markdown.
""".strip()

    def build_PANCE_Verification_prompt(self, q_data):
        return f"""{PANCE_Verification_SYSTEM}
{PANCE_Verification_TASK}
{q_data.get('question_stem')}
A: {q_data.get('answer_choice_A')}
B: {q_data.get('answer_choice_B')}
C: {q_data.get('answer_choice_C')}
D: {q_data.get('answer_choice_D')}
E: {q_data.get('answer_choice_E')}
{PANCE_Verification_CONSTRAINTS}
{PANCE_Verification_OUTPUT}
""".strip()

    def build_PANCE_Difficulty_prompt(self, q_data):
        return f"""{PANCE_DIFFICULTY_SYSTEM}
{PANCE_DIFFICULTY_TASK}
{q_data.get('question_stem')}
A: {q_data.get('answer_choice_A')}
B: {q_data.get('answer_choice_B')}
C: {q_data.get('answer_choice_C')}
D: {q_data.get('answer_choice_D')}
E: {q_data.get('answer_choice_E')}
{PANCE_DIFFICULTY_CONSTRAINTS}
{PANCE_DIFFICULTY_OUTPUT}
""".strip()

# Verification Prompts Constants
PANCE_Verification_SYSTEM = "You are an experienced professor in a Physician Assistant program."
PANCE_Verification_TASK = "Your task is to provide the correct answer to the following question."
PANCE_Verification_CONSTRAINTS = """Follow these rules:
- select the best answer from the choices
- correct answer should correspond to the letter of the answer choice
"""
PANCE_Verification_OUTPUT = """Output your response as valid JSON with this structure:
{
  "correct_answer": "<single letter A-E>",
  "confidence_score": <integer from 1 to 10, where 10 means completely certain>
}
Respond with JSON only. No explanations or markdown.
"""

PANCE_DIFFICULTY_SYSTEM = "You are an experienced professor in a Physician Assistant program."
PANCE_DIFFICULTY_TASK = "Your task is to rate the difficulty of the following question for an average physician assistant student."
PANCE_DIFFICULTY_CONSTRAINTS = """Follow these rules:
- Provide the difficulty of the question on a scale of 1 to 10 with 10 being the hardest. Base rating on likelihood of a physician assistant student to answer correctly.
"""
PANCE_DIFFICULTY_OUTPUT = """Output your response as valid JSON with this structure:
{
  "difficulty": <integer from 1 to 10>
}
Respond with JSON only. No explanations or markdown.
"""

# ==========================================
# ASYNCHRONOUS GENERATION & VERIFICATION
# ==========================================

VERIFICATION_PASSES = 3
MIN_VERIFICATION_CONFIDENCE = 8
# Unusable verifier responses (API errors, empty/malformed JSON, unparseable
# answer/confidence) are retried this many times before the question is discarded.
MAX_UNUSABLE_VERIFIER_RESPONSES = 2
DIFFICULTY_ATTEMPTS = 2
DEFAULT_TOPIC_AREA = "General Medical Knowledge"
_PLACEHOLDER_VALUES = ("str", "string")
_SCORE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(%|/\s*100|/\s*10)?")

def clean_json_response(response_text):
    if not response_text:
        return "{}"
    cleaned = response_text.replace("```json", "").replace("```", "").strip()
    start_idx = cleaned.find('{')
    end_idx = cleaned.rfind('}')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return cleaned[start_idx:end_idx + 1]
    return cleaned

def parse_json_object(response_text):
    """Return (dict, None) on success or (None, reason) for empty/malformed responses."""
    if response_text is None or not str(response_text).strip():
        return None, "empty_response"
    try:
        data = json.loads(clean_json_response(response_text))
    except json.JSONDecodeError:
        return None, "malformed_json"
    if not isinstance(data, dict):
        return None, "malformed_json"
    return data, None

def parse_score(raw_score):
    """Normalize a model score to a float on a 0-10 scale, or None if malformed.

    Accepts ints/floats, numeric strings, percentages ("90%"), "9/10", "90/100",
    and 0-1 fractions (0.9 -> 9.0). Bare values in (10, 100] are treated as percentages.
    """
    if raw_score is None or isinstance(raw_score, bool):
        return None
    if isinstance(raw_score, (int, float)):
        value, suffix = float(raw_score), ""
    elif isinstance(raw_score, str):
        match = _SCORE_PATTERN.fullmatch(raw_score.strip())
        if not match:
            return None
        value, suffix = float(match.group(1)), re.sub(r"\s", "", match.group(2) or "")
    else:
        return None
    if not math.isfinite(value):
        return None
    if suffix in ("%", "/100") or (not suffix and 10 < value <= 100):
        value /= 10
    elif not suffix and 0 < value < 1:
        value *= 10
    return value if 0 <= value <= 10 else None

def _record(stats, reason):
    if stats is not None:
        stats[reason] = stats.get(reason, 0) + 1

def _snippet(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."

def _is_placeholder(value):
    return not value or str(value).strip().lower() in _PLACEHOLDER_VALUES

def standardize_categories(raw_organ, raw_task):
    return standardize_organ_system(raw_organ), standardize_task_area(raw_task)

def extract_correct_letter(raw_text, q_data):
    """Return the answer letter A-E, or None if the text cannot be normalized."""
    if not raw_text:
        return None

    raw_text = str(raw_text).upper().replace("OPTION", "").strip()
    if not raw_text:
        return None

    for letter in ['A', 'B', 'C', 'D', 'E']:
        ans_text = str(q_data.get(f'answer_choice_{letter}', '')).strip().upper()
        if ans_text and ans_text == raw_text:
            return letter

    match = re.search(r'\b([A-E])\b', raw_text)
    if match:
        return match.group(1)

    if raw_text[0] in ['A', 'B', 'C', 'D', 'E']:
        return raw_text[0]

    for letter in ['A', 'B', 'C', 'D', 'E']:
        ans_text = str(q_data.get(f'answer_choice_{letter}', '')).strip().upper()
        if ans_text and (ans_text in raw_text or raw_text in ans_text):
            return letter

    return None

def shuffle_answer_choices(q_data, target_letter=None, label=None):
    # Work on a copy so the caller's (generator's) dict is never mutated.
    q_data = dict(q_data)
    prefix = f"[{label}] " if label else ""
    try:
        choices = {
            'A': q_data.get('answer_choice_A', ''),
            'B': q_data.get('answer_choice_B', ''),
            'C': q_data.get('answer_choice_C', ''),
            'D': q_data.get('answer_choice_D', ''),
            'E': q_data.get('answer_choice_E', '')
        }

        original_correct_letter = extract_correct_letter(q_data.get('correct_answer', ''), q_data)

        if not original_correct_letter:
            print(f"DEBUG: {prefix}Could not parse correct_answer. Skipping shuffle.")
            return q_data

        correct_text = choices.get(original_correct_letter, '')

        if not target_letter:
            target_letter = random.choice(['A', 'B', 'C', 'D', 'E'])

        distractors = [text for letter, text in choices.items() if letter != original_correct_letter]
        random.shuffle(distractors)

        new_choices = {}
        for letter in ['A', 'B', 'C', 'D', 'E']:
            if letter == target_letter:
                new_choices[letter] = correct_text
            else:
                new_choices[letter] = distractors.pop(0)

        q_data['answer_choice_A'] = new_choices['A']
        q_data['answer_choice_B'] = new_choices['B']
        q_data['answer_choice_C'] = new_choices['C']
        q_data['answer_choice_D'] = new_choices['D']
        q_data['answer_choice_E'] = new_choices['E']
        q_data['correct_answer'] = target_letter

        old_exp = str(q_data.get('explanation', '')).strip()
        q_data['explanation'] = f"**Correct Answer: {target_letter}**. \n\n" + old_exp

        print(f"DEBUG: {prefix}Successfully shuffled. Moved correct answer from {original_correct_letter} to {target_letter}.")
        return q_data
    except Exception as e:
        print(f"DEBUG: {prefix}Shuffle failed - {e}")
        return q_data

async def _rate_difficulty(conv_manager_verify, q_data, label, stats):
    reason = None
    for attempt in range(1, DIFFICULTY_ATTEMPTS + 1):
        try:
            prompt = conv_manager_verify.build_PANCE_Difficulty_prompt(q_data)
            response = await conv_manager_verify.chat_completion(prompt, max_tokens=10000)
        except Exception as error:
            reason, detail = "difficulty_api_error", type(error).__name__
        else:
            data, parse_reason = parse_json_object(response)
            if parse_reason:
                reason, detail = f"difficulty_{parse_reason}", f"response={_snippet(response)!r}"
            else:
                raw_difficulty = data.get("difficulty")
                score = parse_score(raw_difficulty)
                difficulty = int(round(score)) if score is not None else None
                if difficulty is not None and 1 <= difficulty <= 10:
                    return difficulty
                reason, detail = "difficulty_malformed", f"difficulty={raw_difficulty!r}"
        print(f"DEBUG: [{label}] Difficulty rating attempt {attempt}/{DIFFICULTY_ATTEMPTS} unusable ({reason}): {detail}")
    _record(stats, reason)
    return None

async def Question_Verification(conv_manager_verify, q_data, label=None, stats=None):
    """Verify a generated question.

    Returns (True, verified_copy) or (False, None). The input dict is never mutated.
    When ``stats`` (a dict) is provided, the reason a question is discarded is counted
    in it, along with ``verifier_responses_retried`` for unusable verifier responses.
    """
    q_data = dict(q_data)
    label = label or f"{q_data.get('organ_system')} / {q_data.get('topic_area')} / {q_data.get('task_area')}"

    raw_generator_answer = q_data.get("correct_answer", "")
    correct_answer = extract_correct_letter(raw_generator_answer, q_data)

    if not correct_answer:
        print(f"DEBUG: [{label}] Verification failed - generator correct_answer {_snippet(raw_generator_answer)!r} could not be normalized to A-E.")
        _record(stats, "generator_answer_unparseable")
        return False, None

    passed = 0
    unusable = 0
    while passed < VERIFICATION_PASSES:
        pass_label = f"pass {passed + 1}/{VERIFICATION_PASSES}"
        reason = detail = None
        try:
            prompt = conv_manager_verify.build_PANCE_Verification_prompt(q_data)
            response = await conv_manager_verify.chat_completion(prompt, max_tokens=10000)
        except Exception as error:
            reason, detail = "verifier_api_error", type(error).__name__
        else:
            extracted_data, parse_reason = parse_json_object(response)
            if parse_reason:
                reason, detail = f"verifier_{parse_reason}", f"response={_snippet(response)!r}"
            else:
                raw_answer = extracted_data.get("correct_answer")
                raw_confidence = extracted_data.get("confidence_score")
                verification_answer = extract_correct_letter(raw_answer, q_data)
                confidence = parse_score(raw_confidence)
                if not verification_answer:
                    reason, detail = "verifier_answer_unparseable", f"answer={_snippet(raw_answer)!r}"
                elif confidence is None:
                    reason, detail = "verifier_confidence_malformed", f"confidence_score={_snippet(repr(raw_confidence))}"

        if reason:
            unusable += 1
            print(f"DEBUG: [{label}] Verification {pass_label} unusable ({reason}): {detail}")
            if unusable > MAX_UNUSABLE_VERIFIER_RESPONSES:
                _record(stats, reason)
                print(f"DEBUG: [{label}] Question failed verification after {unusable} unusable verifier responses. Passed {passed}/{VERIFICATION_PASSES}.")
                return False, None
            _record(stats, "verifier_responses_retried")
            continue

        if verification_answer != correct_answer:
            reason = "verifier_disagreed"
        elif confidence < MIN_VERIFICATION_CONFIDENCE:
            reason = "verifier_low_confidence"
        if reason:
            _record(stats, reason)
            print(f"DEBUG: [{label}] Question failed verification ({reason}) on {pass_label}: "
                  f"generator answer {correct_answer}, verifier answer {verification_answer} "
                  f"(raw {_snippet(raw_answer)!r}), confidence {raw_confidence!r} "
                  f"(normalized {confidence:g}, minimum {MIN_VERIFICATION_CONFIDENCE}). "
                  f"Passed {passed}/{VERIFICATION_PASSES}.")
            return False, None
        passed += 1

    difficulty = await _rate_difficulty(conv_manager_verify, q_data, label, stats)
    if difficulty is None:
        print(f"DEBUG: [{label}] Question passed verification but difficulty rating failed. Discarding.")
        return False, None
    q_data["difficulty_rating"] = difficulty
    q_data["correct_answer"] = correct_answer
    return True, q_data

def _apply_requested_metadata(q_data, organ_system, task_area, topic_area, label):
    """Return a copy whose organ/task/topic come from the request (LLM labels are fallbacks only)."""
    q_data = dict(q_data)
    llm_organ, llm_task, llm_topic = (q_data.get(key) for key in ("organ_system", "task_area", "topic_area"))
    raw_organ = organ_system if not _is_placeholder(organ_system) else (None if _is_placeholder(llm_organ) else llm_organ)
    raw_task = task_area if not _is_placeholder(task_area) else (None if _is_placeholder(llm_task) else llm_task)
    final_organ, final_task = standardize_categories(raw_organ, raw_task)

    if not _is_placeholder(topic_area):
        final_topic = topic_area
    elif not _is_placeholder(llm_topic):
        final_topic = llm_topic
    else:
        final_topic = DEFAULT_TOPIC_AREA

    for field, llm_value, final_value in (("organ_system", llm_organ, final_organ),
                                          ("task_area", llm_task, final_task),
                                          ("topic_area", llm_topic, final_topic)):
        if not _is_placeholder(llm_value) and str(llm_value).strip().casefold() != str(final_value).casefold():
            print(f"DEBUG: [{label}] Generator labeled {field} as {_snippet(llm_value)!r}; using requested {final_value!r}.")

    q_data["organ_system"] = final_organ
    q_data["task_area"] = final_task
    q_data["topic_area"] = final_topic
    return q_data

async def process_topic_batch(item_dict, num_needed, conv_manager, conv_manager_verify, stats=None):
    """Generate and verify up to ``num_needed`` questions for one organ/topic/task request.

    Verified questions are always returned even if later API calls fail. When ``stats``
    (a dict) is provided, discard/failure reasons are counted in it for diagnostics.
    """
    organ_system = item_dict.get("organ_system")
    task_area = item_dict.get("task_area")
    requested_topic = item_dict.get("topic_area")
    topic_area = requested_topic if not _is_placeholder(requested_topic) else DEFAULT_TOPIC_AREA
    summary = item_dict.get("summary")

    if not organ_system:
        return []

    label = f"{organ_system} / {topic_area} / {task_area}"
    verified_questions = []
    attempts = 0
    MAX_ATTEMPTS = 4

    while len(verified_questions) < num_needed and attempts < MAX_ATTEMPTS:
        attempts += 1
        deficit = num_needed - len(verified_questions)
        print(f"DEBUG: [{label}] Generating batch of {deficit} questions (Attempt {attempts})")

        prompt = conv_manager.build_PANCE_batch_prompt(organ_system, topic_area, task_area, deficit, summary)
        try:
            response = await conv_manager.chat_completion(prompt, max_tokens=10000)
        except Exception as error:
            _record(stats, "generator_api_error")
            print(f"DEBUG: [{label}] Generation attempt {attempts} failed ({type(error).__name__}).")
            continue

        batch_data, parse_reason = parse_json_object(response)
        generated_questions = batch_data.get("questions", []) if batch_data else []
        if parse_reason or not isinstance(generated_questions, list):
            _record(stats, f"generator_{parse_reason or 'malformed_json'}")
            print(f"DEBUG: [{label}] Generation attempt {attempts} returned unusable JSON: {_snippet(response)!r}")
            generated_questions = []
        elif len(generated_questions) < deficit:
            print(f"DEBUG: [{label}] Generator returned {len(generated_questions)}/{deficit} requested questions.")

        for index, q_data in enumerate(generated_questions, 1):
            if len(verified_questions) >= num_needed:
                break
            q_label = f"{label} #{attempts}.{index}"
            if not isinstance(q_data, dict):
                _record(stats, "generator_invalid_question")
                print(f"DEBUG: [{q_label}] Generated question is not a JSON object. Discarding.")
                continue

            is_good_question, final_q_data = await Question_Verification(
                conv_manager_verify, q_data, label=q_label, stats=stats)

            if is_good_question:
                final_q_data = _apply_requested_metadata(final_q_data, organ_system, task_area, requested_topic, q_label)
                target_letter = random.choice(['A', 'B', 'C', 'D', 'E'])
                shuffled_q = shuffle_answer_choices(final_q_data, target_letter, label=q_label)
                verified_questions.append(shuffled_q)
                print(f"DEBUG: [{q_label}] Successfully verified 1 question. Total: {len(verified_questions)}/{num_needed}")
            else:
                print(f"DEBUG: [{q_label}] Question failed verification. Discarding.")

    if len(verified_questions) < num_needed:
        details = ""
        if stats:
            details = " Reasons so far: " + ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
        print(f"DEBUG: [{label}] Max attempts reached. Returning {len(verified_questions)}/{num_needed} questions.{details}")

    return verified_questions

async def sem_process_topic_batch(sem, item_dict, num_needed, conv_manager, conv_manager_verify):
    async with sem:
        try:
            return await process_topic_batch(item_dict, num_needed, conv_manager, conv_manager_verify)
        except Exception as e:
            print(f"DEBUG: Batch question generation failed for '{item_dict.get('topic_area')}' - Error: {e}")
            traceback.print_exc()
            return []

async def generate_questions_from_eor_async(input_file_path, quiz_name, output_json_path, username, total_quiz_questions, use_existing, quiz_id=None):
    tasks_to_process = []

    if not os.path.exists(input_file_path):
        print(f"🚨 CRITICAL ERROR: The extracted topics JSON was not found at {input_file_path}")
        raise FileNotFoundError(f"Extracted topics JSON not found: {input_file_path}")

    try:
        with open(input_file_path, 'r') as extraction_file:
            for line in extraction_file:
                if line.strip():
                    try:
                        parsed_data = json.loads(line)
                        if isinstance(parsed_data, dict) and "extracted_topics" in parsed_data:
                            extracted_items = parsed_data["extracted_topics"]
                        else:
                            extracted_items = parsed_data
                        if not isinstance(extracted_items, list):
                            extracted_items = [extracted_items]
                        tasks_to_process.extend(extracted_items)
                    except json.JSONDecodeError as e:
                        print(f"🚨 ERROR parsing JSON on this line: {e} | Line: {line}")
    except Exception as e:
        print(f"🚨 ERROR opening file: {e}")
        raise

    if not tasks_to_process:
        print("🚨 CRITICAL ERROR: File was found, but 0 topics were extracted from it!")
        raise RuntimeError("No topics were extracted from the document.")

    print(f"[{datetime.now()}] Found {len(tasks_to_process)} items to process.")

    for item in tasks_to_process:
        if "percentage" in item:
            pct = item["percentage"] / 100.0
            item["target_count"] = max(1, round(total_quiz_questions * pct))
        else:
            item["target_count"] = total_quiz_questions

    successful_questions = []
    existing_quiz_questions = []
    generation_tasks = []
    conn = None

    conv_manager = ConversationManager(model="gpt-6.1-sol")
    conv_manager_verify = ConversationManager(model="gpt-6.1-sol")
    try:
        conn = await get_async_db_connection()

        sem = asyncio.Semaphore(3)

        async with conn.cursor() as cursor:
            for item in tasks_to_process:
                raw_organ = item.get("organ_system") if isinstance(item, dict) else item.organ_system
                raw_task = item.get("task_area") if isinstance(item, dict) else item.task_area
                topic_area = item.get("topic_area") if isinstance(item, dict) else item.topic_area

                clean_organ, clean_task = standardize_categories(raw_organ, raw_task)
                target_count = item["target_count"]
                fetched_rows = []

                if use_existing:
                    if topic_area and str(topic_area).strip():
                        query = """
                            SELECT id, organ_system, task_area, topic_area, question_stem,
                                   answer_choice_A, answer_choice_B, answer_choice_C,
                                   answer_choice_D, answer_choice_E, correct_answer,
                                   explanation, difficulty_rating
                            FROM question_bank
                            WHERE LOWER(TRIM(organ_system)) = LOWER(%s)
                              AND LOWER(TRIM(topic_area)) = LOWER(%s)
                        """
                        params = [str(clean_organ).strip(), str(topic_area).strip()]

                        if raw_task and str(raw_task).strip():
                            query += " AND LOWER(TRIM(task_area)) = LOWER(%s)"
                            params.append(str(clean_task).strip())

                        query += " LIMIT %s"
                        params.append(target_count)

                        await cursor.execute(query, tuple(params))
                        fetched_rows = await cursor.fetchall()

                    elif raw_task and str(raw_task).strip():
                        await cursor.execute('''
                            SELECT id, organ_system, task_area, topic_area, question_stem,
                                   answer_choice_A, answer_choice_B, answer_choice_C,
                                   answer_choice_D, answer_choice_E, correct_answer,
                                   explanation, difficulty_rating
                            FROM question_bank
                            WHERE LOWER(TRIM(organ_system)) = LOWER(%s)
                              AND LOWER(TRIM(task_area)) = LOWER(%s)
                            LIMIT %s
                        ''', (str(clean_organ).strip(), str(clean_task).strip(), target_count))
                        fetched_rows = await cursor.fetchall()

                    else:
                        await cursor.execute('''
                            SELECT id, organ_system, task_area, topic_area, question_stem,
                                   answer_choice_A, answer_choice_B, answer_choice_C,
                                   answer_choice_D, answer_choice_E, correct_answer,
                                   explanation, difficulty_rating
                            FROM question_bank
                            WHERE LOWER(TRIM(organ_system)) = LOWER(%s)
                            LIMIT %s
                        ''', (str(clean_organ).strip(), target_count))
                        fetched_rows = await cursor.fetchall()

                    for row in fetched_rows:
                        existing_quiz_questions.append({
                            "question_id": row['id'],
                            "clean_organ": row['organ_system'],
                            "clean_task": row['task_area'],
                            "topic_area": row['topic_area'],
                            "question_stem": row['question_stem'],
                            "answer_choice_A": row['answer_choice_A'],
                            "answer_choice_B": row['answer_choice_B'],
                            "answer_choice_C": row['answer_choice_C'],
                            "answer_choice_D": row['answer_choice_D'],
                            "answer_choice_E": row['answer_choice_E'],
                            "correct_answer": row['correct_answer'],
                            "explanation": row['explanation'],
                            "difficulty_rating": row['difficulty_rating'],
                            "created_date": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        })

                    if len(fetched_rows) > 0:
                        print(f"[{datetime.now()}] Mapped {len(fetched_rows)} existing questions for organ: {clean_organ}")

                num_needed = target_count - len(fetched_rows)

                if num_needed > 0:
                    gen_item = {
                        "organ_system": clean_organ,
                        "task_area": clean_task if raw_task else "Formulating the Most Likely Diagnosis",
                        "topic_area": topic_area if (topic_area and str(topic_area).strip()) else "General Medical Knowledge"
                    }
                    generation_tasks.append(sem_process_topic_batch(sem, gen_item, num_needed, conv_manager, conv_manager_verify))

        if conn:
            conn.close()
            conn = None

        if generation_tasks:
            print(f"[{datetime.now()}] Firing {len(generation_tasks)} LLM Topic Batch tasks...")
            for future in asyncio.as_completed(generation_tasks):
                try:
                    results = await future
                    for result in results:
                        successful_questions.append({
                            "question_id": uuid.uuid4().hex,
                            "clean_organ": result.get("organ_system"),
                            "clean_task": result.get("task_area"),
                            "topic_area": result.get("topic_area"),
                            "question_stem": result.get("question_stem"),
                            "answer_choice_A": result.get("answer_choice_A"),
                            "answer_choice_B": result.get("answer_choice_B"),
                            "answer_choice_C": result.get("answer_choice_C"),
                            "answer_choice_D": result.get("answer_choice_D"),
                            "answer_choice_E": result.get("answer_choice_E"),
                            "correct_answer": result.get("correct_answer"),
                            "explanation": result.get("explanation"),
                            "difficulty_rating": result.get("difficulty_rating"),
                            "created_date": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        })
                except Exception as e:
                    print(f"DEBUG: Task error: {e}")

        all_quiz_questions = existing_quiz_questions + successful_questions
        if not all_quiz_questions:
            raise RuntimeError("Document generation produced no questions.")

        if all_quiz_questions:
            conn = await get_async_db_connection()

            async with conn.cursor() as cursor:
                if not quiz_id:
                    await cursor.execute('SELECT quiz_id FROM user_quizzes WHERE username = %s AND quiz_name = %s', (username, quiz_name))
                    quiz_row = await cursor.fetchone()

                    if quiz_row:
                        quiz_id = quiz_row['quiz_id']
                    else:
                        quiz_id = uuid.uuid4().hex
                        await cursor.execute('''
                            INSERT IGNORE INTO user_quizzes (quiz_id, username, quiz_name, created_date)
                            VALUES (%s, %s, %s, %s)
                        ''', (quiz_id, username, quiz_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

                if successful_questions:
                    print(f"[{datetime.now()}] Bulk Inserting {len(successful_questions)} new AI questions to Database.")

                    insert_data = [
                        (
                            q["question_id"], q["clean_organ"], q["clean_task"], q["topic_area"], q["question_stem"],
                            q["answer_choice_A"], q["answer_choice_B"], q["answer_choice_C"], q["answer_choice_D"], q["answer_choice_E"],
                            q["correct_answer"], q["explanation"], q["difficulty_rating"], q["created_date"]
                        ) for q in successful_questions
                    ]

                    await cursor.executemany('''
                        INSERT IGNORE INTO question_bank (
                            id, organ_system, task_area, topic_area, question_stem,
                            answer_choice_A, answer_choice_B, answer_choice_C,
                            answer_choice_D, answer_choice_E, correct_answer,
                            explanation, difficulty_rating, created_date,
                            global_thumbs_up, global_thumbs_down
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0)
                    ''', insert_data)

                if all_quiz_questions:
                    print(f"[{datetime.now()}] Bulk Mapping all {len(all_quiz_questions)} questions to Quiz ID: {quiz_id}")

                    mapping_data = []
                    for q in all_quiz_questions:
                        question_id_to_map = q["question_id"]

                        if q in successful_questions and cursor.rowcount < len(successful_questions):
                            await cursor.execute('SELECT id FROM question_bank WHERE question_stem = %s', (q["question_stem"],))
                            existing_row = await cursor.fetchone()
                            if existing_row:
                                question_id_to_map = existing_row['id']

                        mapping_data.append((quiz_id, question_id_to_map))

                    if mapping_data:
                        await cursor.executemany('''
                            INSERT IGNORE INTO quiz_questions_map (quiz_id, question_id)
                            VALUES (%s, %s)
                        ''', mapping_data)

                await conn.commit()

            if output_json_path:
                with open(output_json_path, 'w') as f:
                    json.dump(all_quiz_questions, f, indent=4)

    except Exception as e:
        print(f"DEBUG: Error processing generation or database: {e}")
        raise
    finally:
        if conn:
            conn.close()
        await conv_manager.client.close()
        await conv_manager_verify.client.close()

def generate_questions_from_eor(input_file_path, quiz_name, output_json_path, username, total_quiz_questions, use_existing, quiz_id=None):
    return asyncio.run(generate_questions_from_eor_async(input_file_path, quiz_name, output_json_path, username, total_quiz_questions, use_existing, quiz_id))