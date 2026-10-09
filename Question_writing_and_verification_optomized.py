####Question_writing_and_verification_optomized.py
import os
import json
import aiomysql
import re
import asyncio
import uuid
import random
import traceback
import difflib
from openai import AsyncOpenAI
from datetime import datetime
from dotenv import load_dotenv

# Point exactly to where your .env file lives
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# Fetch keys & DB credentials
api_key = os.getenv('MY_API_KEY')
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

if not os.getenv('OPENAI_API_KEY') and not api_key:
    print("🚨 ERROR: Could not find OPENAI_API_KEY in the .env file!")

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_SYSTEM_MESSAGE = "You are an experienced professor in a Physician Assistant program."

class ConversationManager:
    def __init__(self, api_key=None, model=None, max_tokens=None, token_budget=None, system_message=None):
        self.client = AsyncOpenAI(api_key=api_key or os.getenv('OPENAI_API_KEY'))
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
  "correct_answer": "str",
  "confidence_score": "int"
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
  "difficulty": "int"
}
Respond with JSON only. No explanations or markdown.
"""

# ==========================================
# ASYNCHRONOUS GENERATION & VERIFICATION
# ==========================================

def clean_json_response(response_text):
    if not response_text:
        return "{}"
    cleaned = response_text.replace("```json", "").replace("```", "").strip()
    start_idx = cleaned.find('{')
    end_idx = cleaned.rfind('}')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return cleaned[start_idx:end_idx + 1]
    return cleaned

def standardize_categories(raw_organ, raw_task):
    exact_organs = [
        "Cardiovascular System", "Dermatologic System", "Endocrine System",
        "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
        "Genitourinary System", "Hematologic System", "Infectious Diseases",
        "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
        "Pulmonary System", "Renal System", "Reproductive System"
    ]

    exact_tasks = [
        "History Taking and Performing Physical Examination",
        "Using Diagnostic and Laboratory Studies",
        "Formulating the Most Likely Diagnosis",
        "Health Maintenance, Patient Education, and Preventive Measures",
        "Clinical Intervention",
        "Pharmaceutical Therapeutics",
        "Applying Foundational Scientific Concepts"
    ]

    raw_organ_str = str(raw_organ).strip() if raw_organ else ""
    raw_task_str = str(raw_task).strip() if raw_task else ""

    # 🚨 FIX 1: Extract category prefix if whole line was passed into raw_organ (e.g., "Hematology: Diagnostic Studies...")
    if ":" in raw_organ_str:
        prefix = raw_organ_str.split(":")[0].strip()
        organ_lower = prefix.lower()
    else:
        organ_lower = raw_organ_str.lower()

    # 🚨 FIX 2: Explicit Intercepts (Prioritized before fuzzy matching)
    if any(term in organ_lower for term in ["hematolog", "heme", "blood", "hemato", "myeloma", "anemia", "b12"]):
        clean_organ = "Hematologic System"
    elif any(term in organ_lower for term in ["neurolog", "neuro", "brain", "parkinson", "seizure", "dementia"]):
        clean_organ = "Neurologic System"
    elif any(term in organ_lower for term in ["gynecolog", "reproductive", "obstetric", "ob/gyn", "obgyn", "women", "female", "pregnancy"]):
        clean_organ = "Reproductive System"
    elif any(term in organ_lower for term in ["eenot", "eent", "ear", "eye", "throat", "nose", "oral cavity", "ent", "glaucoma"]):
        clean_organ = "Eyes, Ears, Nose, and Throat"
    elif any(term in organ_lower for term in ["gastrointestinal", "gi", "gastro", "nutrition", "stomach", "bowel", "esophag", "ulcerative"]):
        clean_organ = "Gastrointestinal System/Nutrition"
    elif any(term in organ_lower for term in ["cardiovascular", "cardio", "heart", "vascular", "tamponade", "tachycardia"]):
        clean_organ = "Cardiovascular System"
    elif any(term in organ_lower for term in ["dermatolog", "derm", "skin"]):
        clean_organ = "Dermatologic System"
    elif any(term in organ_lower for term in ["psychiat", "behavior", "psych", "mental"]):
        clean_organ = "Psychiatry/Behavioral Science"
    elif any(term in organ_lower for term in ["rheumatolog", "musculoskeletal", "ortho", "bone", "joint", "musculo", "lupus", "stenosis"]):
        clean_organ = "Musculoskeletal System"
    elif any(term in organ_lower for term in ["infectious", "infec", "salmonella"]):
        clean_organ = "Infectious Diseases"
    elif any(term in organ_lower for term in ["endocrine", "endo", "thyroid", "diabetes"]):
        clean_organ = "Endocrine System"
    elif any(term in organ_lower for term in ["pulmonary", "pulm", "resp", "asthma", "pneumonia", "sarcoidosis"]):
        clean_organ = "Pulmonary System"
    elif "renal" in organ_lower and "genitourinary" not in organ_lower:
        clean_organ = "Renal System"
    elif "genitourinary" in organ_lower and "renal" not in organ_lower:
        clean_organ = "Genitourinary System"
    elif "renal" in organ_lower or "genitourinary" in organ_lower:
        clean_organ = "Genitourinary System"
    else:
        clean_organ = raw_organ_str.title()

    # 🚨 FIX 3: Clean up Task Area Extraction
    task_lower = raw_task_str.lower()
    if any(term in task_lower for term in ["lab", "diagnostic", "study"]):
        clean_task = "Using Diagnostic and Laboratory Studies"
    elif any(term in task_lower for term in ["pharm", "medication", "drug", "therapeutics", "treatment"]):
        clean_task = "Pharmaceutical Therapeutics"
    elif any(term in task_lower for term in ["history", "physical", "exam"]):
        clean_task = "History Taking and Performing Physical Examination"
    elif any(term in task_lower for term in ["prevent", "educat", "health maintenance", "maintenance"]):
        clean_task = "Health Maintenance, Patient Education, and Preventive Measures"
    elif any(term in task_lower for term in ["intervention", "procedure", "surgery"]):
        clean_task = "Clinical Intervention"
    elif any(term in task_lower for term in ["scientific", "concept", "pathophysiology"]):
        clean_task = "Applying Foundational Scientific Concepts"
    else:
        clean_task = raw_task_str.title()

    organ_match = difflib.get_close_matches(clean_organ, exact_organs, n=1, cutoff=0.3)
    task_match = difflib.get_close_matches(clean_task, exact_tasks, n=1, cutoff=0.3)

    # 🚨 Updated Fallback: Tag unmatched organ systems as "Misc"
    if organ_match:
        final_organ = organ_match[0]
    elif clean_organ in exact_organs:
        final_organ = clean_organ
    else:
        final_organ = "Misc"

    final_task = task_match[0] if task_match else (clean_task if clean_task in exact_tasks else "Formulating the Most Likely Diagnosis")

    return final_organ, final_task

def extract_correct_letter(raw_text, q_data):
    if not raw_text:
        return 'A'

    raw_text = str(raw_text).upper().replace("OPTION", "").strip()

    match = re.search(r'\b([A-E])\b', raw_text)
    if match:
        return match.group(1)

    if len(raw_text) > 0 and raw_text[0] in ['A', 'B', 'C', 'D', 'E']:
        return raw_text[0]

    for letter in ['A', 'B', 'C', 'D', 'E']:
        ans_text = str(q_data.get(f'answer_choice_{letter}', '')).strip().upper()
        if ans_text and (ans_text in raw_text or raw_text in ans_text):
            return letter

    return 'A'

def shuffle_answer_choices(q_data, target_letter=None):
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
            print(f"DEBUG: Could not parse correct_answer. Skipping shuffle.")
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

        print(f"DEBUG: Successfully shuffled. Moved correct answer from {original_correct_letter} to {target_letter}.")
        return q_data
    except Exception as e:
        print(f"DEBUG: Shuffle failed - {e}")
        return q_data

async def Question_Verification(conv_manager_verify, q_data):
    verification_count = 0
    correct_count = 0

    correct_answer = extract_correct_letter(q_data.get("correct_answer", ""), q_data)

    if not correct_answer:
        print(f"DEBUG: Verification Failed - Generator correct_answer is improperly formatted.")
        return False, None

    while verification_count < 3:
        prompt = conv_manager_verify.build_PANCE_Verification_prompt(q_data)
        response = await conv_manager_verify.chat_completion(prompt, max_tokens=10000)
        try:
            extracted_data = json.loads(clean_json_response(response))
            verification_answer = extract_correct_letter(extracted_data.get("correct_answer", ""), q_data)

            if not verification_answer:
                verification_count += 1
                continue

            raw_confidence = extracted_data.get("confidence_score", 0)
            try:
                confidence = int(raw_confidence)
            except (ValueError, TypeError):
                confidence = 0

            if verification_answer == correct_answer and confidence >= 8:
                correct_count += 1
        except json.JSONDecodeError:
            pass

        verification_count += 1

    if correct_count == 3:
        prompt = conv_manager_verify.build_PANCE_Difficulty_prompt(q_data)
        response = await conv_manager_verify.chat_completion(prompt, max_tokens=10000)
        try:
            extracted_data = json.loads(clean_json_response(response))
            q_data["difficulty_rating"] = extracted_data.get("difficulty")
            q_data["correct_answer"] = correct_answer
            return True, q_data
        except json.JSONDecodeError:
            return False, None

    print(f"DEBUG: Question failed verification. Correct count: {correct_count}")
    return False, None

async def process_topic_batch(item_dict, num_needed, conv_manager, conv_manager_verify):
    organ_system = item_dict.get("organ_system")
    task_area = item_dict.get("task_area")
    topic_area = item_dict.get("topic_area", "General Medical Knowledge")
    summary = item_dict.get("summary")

    if not organ_system:
        return []

    verified_questions = []
    attempts = 0
    MAX_ATTEMPTS = 4

    while len(verified_questions) < num_needed and attempts < MAX_ATTEMPTS:
        deficit = num_needed - len(verified_questions)
        print(f"DEBUG: Generating batch of {deficit} questions for {topic_area} ({organ_system}) (Attempt {attempts + 1})")

        prompt = conv_manager.build_PANCE_batch_prompt(organ_system, topic_area, task_area, deficit, summary)
        response = await conv_manager.chat_completion(prompt, max_tokens=10000)

        try:
            batch_data = json.loads(clean_json_response(response))
            generated_questions = batch_data.get("questions", [])
        except json.JSONDecodeError:
            generated_questions = []

        for q_data in generated_questions:
            if len(verified_questions) >= num_needed:
                break

            is_good_question, final_q_data = await Question_Verification(conv_manager_verify, q_data)

            if is_good_question:
                llm_organ = final_q_data.get("organ_system")
                llm_task = final_q_data.get("task_area")

                if not llm_organ or str(llm_organ).lower() in ["str", "string"]:
                    final_q_data["organ_system"] = organ_system
                else:
                    std_organ, _ = standardize_categories(llm_organ, llm_task)
                    final_q_data["organ_system"] = std_organ

                if not llm_task or str(llm_task).lower() in ["str", "string"]:
                    final_q_data["task_area"] = task_area
                else:
                    _, std_task = standardize_categories(llm_organ, llm_task)
                    final_q_data["task_area"] = std_task

                if not final_q_data.get("topic_area") or str(final_q_data.get("topic_area")).lower() in ["str", "string"]:
                    final_q_data["topic_area"] = topic_area

                target_letter = random.choice(['A', 'B', 'C', 'D', 'E'])
                shuffled_q = shuffle_answer_choices(final_q_data, target_letter)
                verified_questions.append(shuffled_q)
                print(f"DEBUG: Successfully verified 1 question for {topic_area}. Total: {len(verified_questions)}/{num_needed}")
            else:
                print(f"DEBUG: A question for {topic_area} failed verification. Discarding.")

        attempts += 1

    if len(verified_questions) < num_needed:
        print(f"DEBUG: Max attempts reached for {topic_area}. Returning {len(verified_questions)}/{num_needed} questions.")

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
    conv_manager = ConversationManager(model="gpt-6.1-sol")
    conv_manager_verify = ConversationManager(model="gpt-6.1-sol")
    tasks_to_process = []

    if not os.path.exists(input_file_path):
        print(f"🚨 CRITICAL ERROR: The extracted topics JSON was not found at {input_file_path}")
        return

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
        return

    if not tasks_to_process:
        print("🚨 CRITICAL ERROR: File was found, but 0 topics were extracted from it!")
        return

    print(f"[{datetime.now()}] Found {len(tasks_to_process)} items to process.")

    for item in tasks_to_process:
        pct = item.get("percentage", 0) / 100.0
        item["target_count"] = max(1, round(total_quiz_questions * pct))

    successful_questions = []
    existing_quiz_questions = []
    generation_tasks = []
    conn = None

    try:
        conn = await aiomysql.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            db=MYSQL_DB,
            cursorclass=aiomysql.DictCursor
        )

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

        if all_quiz_questions:
            conn = await aiomysql.connect(
                host=MYSQL_HOST,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                db=MYSQL_DB,
                cursorclass=aiomysql.DictCursor
            )

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
    finally:
        if conn:
            conn.close()
        await conv_manager.client.close()
        await conv_manager_verify.client.close()

def generate_questions_from_eor(input_file_path, quiz_name, output_json_path, username, total_quiz_questions, use_existing, quiz_id=None):
    return asyncio.run(generate_questions_from_eor_async(input_file_path, quiz_name, output_json_path, username, total_quiz_questions, use_existing, quiz_id))