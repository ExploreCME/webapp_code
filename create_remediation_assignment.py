#create_remediation_assignment.py
#create_remediation_assignment.py
import os
import json
import asyncio
import uuid
import aiomysql
import traceback
import difflib
from datetime import datetime
from openai import AsyncOpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import List, Literal

# --- IMPORT THE UNIFIED ENGINE FROM YOUR CORE SCRIPT ---
from Question_writing_and_verification_optomized import (
    standardize_categories,
    process_topic_batch,
    ConversationManager as CoreConversationManager
)

load_dotenv('/home/ps51632/mysite/explorecme/.env')

DEFAULT_MODEL = "gpt-6.1-sol"
SYSTEM_MESSAGE = "You are an experienced professor in a Physician Assistant program."

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

# ==========================================
# PYDANTIC SCHEMAS FOR STRUCTURED OUTPUT
# ==========================================
class EORExtractionItem(BaseModel):
    organ_system: Literal[
        "Cardiovascular System",
        "Dermatologic System",
        "Endocrine System",
        "Eyes, Ears, Nose, and Throat",
        "Gastrointestinal System/Nutrition",
        "Genitourinary System",
        "Hematologic System",
        "Infectious Diseases",
        "Musculoskeletal System",
        "Neurologic System",
        "Psychiatry/Behavioral Science",
        "Pulmonary System",
        "Renal System",
        "Reproductive System",
        "Professional Practice",
        "Misc"
    ] = Field(description="Must be EXACTLY one of the constrained PANCE categories or Misc")

    task_area: Literal[
        "History Taking and Performing Physical Examination",
        "Using Laboratory and Diagnostic Studies",
        "Formulating Most Likely Diagnosis",
        "Managing Patients - Health Maintenance, Patient Education, and Preventive Measures",
        "Managing Patients - Clinical Intervention",
        "Managing Patients - Pharmaceutical Therapeutics",
        "Applying Foundational Scientific Concepts"
    ] = Field(description="Must be EXACTLY one of the constrained PANCE task areas")

    topic_area: str = Field(description="The specific medical condition, disease, or keyword extracted from the document (e.g., 'Asthma', 'Hypertension'). NEVER put a task area like 'Clinical Intervention' here.")

class EORResponse(BaseModel):
    extracted_topics: List[EORExtractionItem]

EXTRACTION_PROMPT = """You are an experienced professor in a Physician Assistant program.
Your task is to evaluate the given file to identify the content category, task area, and keywords listed under the Keywords section.

Follow these rules:
- You must extract three distinct pieces of information for each entry:
  1. organ_system (Content Category)
  2. task_area (Task Area)
  3. topic_area (Keywords / Specific Medical Conditions)
- CRITICAL MAPPING: The "topic_area" MUST contain the specific diseases, medical conditions, or keywords listed in the document (e.g., "Acute bronchitis", "Hypertension"). Do NOT duplicate the task area into the topic area field.
- CRITICAL: You MUST standardize the "organ_system" by mapping it to EXACTLY one of the following categories:
  * Cardiovascular System
  * Dermatologic System
  * Endocrine System
  * Eyes, Ears, Nose, and Throat
  * Gastrointestinal System/Nutrition
  * Genitourinary System
  * Hematologic System
  * Infectious Diseases
  * Musculoskeletal System
  * Neurologic System
  * Psychiatry/Behavioral Science
  * Pulmonary System
  * Renal System
  * Reproductive System
  * Professional Practice
  * Misc
- CRITICAL: You MUST standardize the "task_area" by mapping it to EXACTLY one of the following categories:
  * History Taking and Performing Physical Examination
  * Using Laboratory and Diagnostic Studies
  * Formulating Most Likely Diagnosis
  * Managing Patients - Health Maintenance, Patient Education, and Preventive Measures
  * Managing Patients - Clinical Intervention
  * Managing Patients - Pharmaceutical Therapeutics
  * Applying Foundational Scientific Concepts
""".strip()

SUMMARY_GEN_PROMPT = """As a Physician Assistant educator, generate a high-yield, highly structured study guide for the following topic, designed to be displayed on a modern interactive flashcard.

Organ System: {organ_system}
Task Area: {task_area}
Topic: {topic_area}

Format the summary using Markdown. Avoid dense walls of text; prioritize readability, active recall, and quick retention. Use clean headers, bullet points, and bold text for key concepts. Break the content down into the following core sections:
- 📋 **Clinical Presentation** (Key signs, symptoms, and classic patient demographic)
- 🔬 **Diagnosis** (Initial workup vs. Gold standard test)
- 💊 **Management** (First-line treatments, medications, and alternatives)
- 🚨 **High-Yield Pearls** (Crucial "can't miss" PANCE facts and red flags)

The content must be medically accurate, appropriately leveled for PA students, and heavily emphasize the specific '{task_area}' requested.

Output as valid JSON with all Markdown appropriately escaped:
{{
    "summary": "your Markdown-formatted summary string here"
}}"""

SUMMARY_VERIFY_PROMPT = """Review the following summary for medical accuracy and relevance to PA education.
Organ System: {organ_system}
Task Area: {task_area}
Topic: {topic_area}

Summary to verify:
{summary}

Is this summary accurate and free of hallucinations or dangerous medical advice?
Output as valid JSON:
{{
    "is_valid": true/false,
    "confidence_score": 1-10,
    "feedback": "brief reasoning"
}}"""

DIAGRAM_GEN_PROMPT = """Based on the following topic and summary, evaluate if a learning mnemonic would be highly beneficial for knowledge retention of the high-value topics identified in the summary.

If a mnemonic makes sense for the topic, create a schematic visual learning aid using a Mermaid.js flowchart diagram. This visual aid MUST focus on a SIMPLIFIED, easy-to-remember learning mnemonic. The mnemonic must be based on the summary content and focus ONLY on high-value, high-yield information essential for core disease understanding. Structure the flowchart to visually break down the mnemonic letter-by-letter, with concise, high-yield explanations for each.

IMPORTANT VISUAL REQUIREMENT: Format the key letters or words to stand out clearly (e.g., HTML bold tags like <b>M</b>). Use bullet points INSIDE the diagram's node labels (using '•' and '<br>').

Additionally, generate a concise textual explanation that goes along with the mnemonic/schematic.

If a mnemonic or visual diagram does NOT make sense or would be too forced, return an empty string for both the code and explanation.

Organ System: {organ_system}
Task Area: {task_area}
Topic: {topic_area}
Summary: {summary}

Output as valid JSON:
{{
    "diagram_code": "graph TD\\n  A[<b>M</b>nemonic] --> B[<b>L</b>etter 1: Core Concept<br>• Bullet point 1]",
    "diagram_explanation": "A brief explanation of how the mnemonic works."
}}"""

DIAGRAM_VERIFY_PROMPT = """Review the following learning mnemonic/schematic for medical accuracy, logical coherence, and educational value for a PA student.

Topic: {topic_area} ({organ_system} - {task_area})
Summary Context: {summary}

Mnemonic/Diagram Code: {diagram_code}
Explanation: {diagram_explanation}

Does this mnemonic genuinely make sense? Is it a forced/confusing acronym, or does it serve as an effective, easy-to-remember teaching tool?

Output as valid JSON:
{{
    "is_valid": true/false,
    "confidence_score": 1-10,
    "feedback": "brief reasoning"
}}"""

# ==========================================
# HELPER FUNCTIONS & CLASSES
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

class ConversationManager:
    """Local manager used specifically for the Pydantic Extraction and Summaries"""
    def __init__(self, model=DEFAULT_MODEL):
        self.client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY') or os.getenv('MY_API_KEY'))
        self.model = model
        self.system_message = SYSTEM_MESSAGE

    async def chat_completion(self, prompt, max_tokens=10000):
        messages = [
            {"role": "system", "content": self.system_message},
            {"role": "user", "content": prompt}
        ]
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        try:
            return json.loads(clean_json_response(response.choices[0].message.content))
        except json.JSONDecodeError:
            return None

    async def extract_from_file(self, uploaded_file_id: str):
        messages = [
            {"role": "system", "content": self.system_message},
            {"role": "user", "content": [
                {"type": "text", "text": EXTRACTION_PROMPT},
                {"type": "file", "file": {"file_id": uploaded_file_id}}
            ]}
        ]
        response = await self.client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.4,
            response_format=EORResponse
        )
        extracted_topics = response.choices[0].message.parsed.extracted_topics

        # Load canonical topics JSON for matching (identical to EOR_Extraction_optimized)
        json_path = "/home/ps51632/mysite/explorecme/topics_by_organ_system.json"
        try:
            with open(json_path, 'r') as f:
                canonical_topics_data = json.load(f)
        except FileNotFoundError:
            print(f"Warning: Could not find {json_path}. Proceeding without fuzzy matching.")
            canonical_topics_data = {}

        # Standardize categories and fuzzy match topic area against canonical JSON
        for item in extracted_topics:
            clean_organ, clean_task = standardize_categories(item.organ_system, item.task_area)
            item.organ_system = clean_organ
            item.task_area = clean_task

            raw_topic = item.topic_area
            valid_topics = canonical_topics_data.get(clean_organ, [])

            if valid_topics and raw_topic:
                matches = difflib.get_close_matches(raw_topic, valid_topics, n=1, cutoff=0.6)
                if matches:
                    print(f"Matched raw '{raw_topic}' -> Canonical '{matches[0]}'")
                    item.topic_area = matches[0]
                else:
                    print(f"No close match found for '{raw_topic}' in {clean_organ}. Keeping raw string.")

        return extracted_topics

# ==========================================
# ASYNCHRONOUS GENERATION & VERIFICATION
# ==========================================
async def process_topic_set(manager, manager_verify, item):
    """Processes a single topic, utilizing fallback logic if verification fails."""
    clean_organ, clean_task = standardize_categories(item.organ_system, item.task_area)
    item_dict = {
        "organ_system": clean_organ,
        "task_area": clean_task,
        "topic_area": item.topic_area
    }

    # 1. Generate & Verify Summary with Fallback
    summary = None
    last_summary = None
    for _ in range(3):
        gen_data = await manager.chat_completion(SUMMARY_GEN_PROMPT.format(**item_dict))
        if not gen_data or "summary" not in gen_data: continue
        last_summary = gen_data["summary"]
        verify_data = await manager_verify.chat_completion(SUMMARY_VERIFY_PROMPT.format(**item_dict, summary=last_summary))
        if verify_data and verify_data.get("is_valid") and verify_data.get("confidence_score", 0) >= 8:
            summary = last_summary
            break
    if not summary and last_summary:
        print(f"DEBUG: Summary verification failed 3 times for {item.topic_area}. Using fallback.")
        summary = last_summary
    if not summary: return None
    item_dict["summary"] = summary

    # 2. Generate & Verify Diagram with Fallback
    diagram_code, diagram_exp = "", ""
    last_code, last_exp = "", ""
    for _ in range(3):
        d_data = await manager.chat_completion(DIAGRAM_GEN_PROMPT.format(**item_dict))
        if not d_data or not d_data.get("diagram_code"):
            break
        last_code = d_data.get("diagram_code", "")
        last_exp = d_data.get("diagram_explanation", "")
        v_payload = {**item_dict, "diagram_code": last_code, "diagram_explanation": last_exp}
        v_data = await manager_verify.chat_completion(DIAGRAM_VERIFY_PROMPT.format(**v_payload))
        if v_data and v_data.get("is_valid") and v_data.get("confidence_score", 0) >= 8:
            diagram_code, diagram_exp = last_code, last_exp
            break
    if not diagram_code and last_code:
        print(f"DEBUG: Diagram verification failed 3 times for {item.topic_area}. Using fallback.")
        diagram_code, diagram_exp = last_code, last_exp
    item_dict["diagram_code"] = diagram_code
    item_dict["diagram_explanation"] = diagram_exp

    # 3. Generate Questions (100% Unified with EOR/On-Demand batch logic)
    core_manager = CoreConversationManager(model="gpt-6.1-sol")
    core_manager_verify = CoreConversationManager(model="gpt-6.1-sol")
    try:
        raw_questions = await process_topic_batch(
            item_dict=item_dict,
            num_needed=5,
            conv_manager=core_manager,
            conv_manager_verify=core_manager_verify
        )
        verified_questions = []
        for q in raw_questions:
            raw_q_organ = q.get("organ_system")
            if not raw_q_organ or str(raw_q_organ).lower() in ["str", "string", "none", "misc", "unassigned"]:
                raw_q_organ = item_dict["organ_system"]
            raw_q_task = q.get("task_area")
            if not raw_q_task or str(raw_q_task).lower() in ["str", "string", "none"]:
                raw_q_task = item_dict["task_area"]
            q_clean_organ, q_clean_task = standardize_categories(raw_q_organ, raw_q_task)
            q["clean_organ"] = q_clean_organ
            q["clean_task"] = q_clean_task
            q["organ_system"] = q_clean_organ
            q["task_area"] = q_clean_task
            verified_questions.append(q)
    finally:
        await core_manager.client.close()
        await core_manager_verify.client.close()

    item_dict["questions"] = verified_questions
    return item_dict

async def sem_process_topic_set(sem, manager, manager_verify, item):
    async with sem:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = await process_topic_set(manager, manager_verify, item)
                if result:
                    return result
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"DEBUG: Module '{item.topic_area}' dropped (Attempt {attempt + 1}/{max_retries}). Retrying in 5s... Error: {e}")
                    await asyncio.sleep(5)
                else:
                    print(f"DEBUG: Module '{item.topic_area}' completely failed after {max_retries} attempts.")
                    traceback.print_exc()
                    raise

# ==========================================
# DATABASE & BACKGROUND PROCESSING
# ==========================================
async def check_existing_remediation(conn, organ_system: str, task_area: str, topic_area: str):
    async with conn.cursor() as cursor:
        await cursor.execute('''
            SELECT id, summary, diagram_code, diagram_explanation
            FROM remediation_assignments
            WHERE LOWER(TRIM(organ_system))=LOWER(TRIM(%s))
              AND LOWER(TRIM(task_area))=LOWER(TRIM(%s))
              AND LOWER(TRIM(topic_area))=LOWER(TRIM(%s))
            LIMIT 1
        ''', (organ_system, task_area, topic_area))
        existing_assignment = await cursor.fetchone()
        if existing_assignment:
            return {"id": existing_assignment["id"]}
    return None

async def save_to_database(username: str, data_list: list, remediation_name: str):
    conn = await aiomysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        charset='utf8mb4',
        cursorclass=aiomysql.DictCursor
    )
    generated_ids = []
    try:
        async with conn.cursor() as cursor:
            assignment_tuples = []
            question_tuples = []
            current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for data in data_list:
                if not data: continue
                if "id" in data:
                    generated_ids.append(data["id"])
                    continue
                assignment_id = uuid.uuid4().hex
                generated_ids.append(assignment_id)
                assignment_organ = data.get("organ_system") or "Unassigned"
                assignment_task = data.get("task_area") or "Formulating the Most Likely Diagnosis"
                assignment_topic = data.get("topic_area") or "General"
                assignment_tuples.append((
                    assignment_id, username, remediation_name,
                    assignment_organ,
                    assignment_task,
                    assignment_topic,
                    data.get("summary", ""),
                    data.get("diagram_code", ""),
                    data.get("diagram_explanation", ""),
                    current_date
                ))
                for q in data.get("questions", []):
                    question_id = uuid.uuid4().hex
                    question_tuples.append((
                        question_id, assignment_id,
                        q.get("question_stem", ""),
                        q.get("answer_choice_A", ""),
                        q.get("answer_choice_B", ""),
                        q.get("answer_choice_C", ""),
                        q.get("answer_choice_D", ""),
                        q.get("answer_choice_E", ""),
                        q.get("correct_answer", "A"),
                        q.get("explanation", ""),
                        q.get("difficulty_rating", 5)
                    ))
            if assignment_tuples:
                print(f"[{datetime.now()}] Bulk Inserting {len(assignment_tuples)} Remediation Flashcard Modules.")
                await cursor.executemany('''
                    INSERT INTO remediation_assignments
                    (id, username, remediation_name, organ_system, task_area, topic_area, summary, diagram_code, diagram_explanation, created_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', assignment_tuples)
            if question_tuples:
                print(f"[{datetime.now()}] Bulk Inserting {len(question_tuples)} Remediation Questions.")
                await cursor.executemany('''
                    INSERT INTO remediation_questions
                    (id, assignment_id, question_stem, answer_choice_A, answer_choice_B, answer_choice_C, answer_choice_D, answer_choice_E, correct_answer, explanation, difficulty_rating)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', question_tuples)
        await conn.commit()
    except Exception as e:
        print(f"DEBUG: Error in bulk save_to_database: {e}")
    finally:
        conn.close()
    return generated_ids

async def notify_user(username: str, remediation_name: str, status: str, payload=None):
    if status == "success":
        print(f"NOTIFICATION: {username}, your remediation '{remediation_name}' is ready! IDs: {payload}")
    else:
        print(f"NOTIFICATION: {username}, remediation '{remediation_name}' failed. Error: {payload}")

async def background_process_document(filepath: str, username: str, remediation_name: str):
    manager = ConversationManager()
    manager_verify = ConversationManager()
    uploaded_file = None
    db_conn = None
    try:
        db_conn = await aiomysql.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            db=MYSQL_DB,
            charset='utf8mb4',
            cursorclass=aiomysql.DictCursor
        )
        with open(filepath, "rb") as file_data:
            uploaded_file = await manager.client.files.create(file=file_data, purpose="user_data")
        extracted_topics = await manager.extract_from_file(uploaded_file.id)
        final_ids = []
        generation_tasks = []
        sem = asyncio.Semaphore(3)
        for item in extracted_topics:
            clean_organ, clean_task = standardize_categories(item.organ_system, item.task_area)
            item.organ_system = clean_organ
            item.task_area = clean_task
            existing_data = await check_existing_remediation(db_conn, item.organ_system, item.task_area, item.topic_area)
            if existing_data:
                final_ids.append(existing_data["id"])
            else:
                generation_tasks.append(sem_process_topic_set(sem, manager, manager_verify, item))
        if db_conn:
            db_conn.close()
            db_conn = None
        new_results = []
        if generation_tasks:
            print(f"DEBUG: Generating {len(generation_tasks)} new remediation modules offline...")
            for future in asyncio.as_completed(generation_tasks):
                try:
                    result = await future
                    if result:
                        new_results.append(result)
                except Exception as e:
                    print(f"DEBUG: Individual module generation failed - {e}")
                    traceback.print_exc()
        if new_results:
            saved_ids = await save_to_database(username, new_results, remediation_name)
            final_ids.extend(saved_ids)
        if final_ids:
            await notify_user(username, remediation_name, "success", final_ids)
            return final_ids
        else:
            print(f"DEBUG: No results to save for {remediation_name}.")
            return []
    except Exception as e:
        print(f"CRITICAL ERROR in background_process_document: {e}")
        traceback.print_exc()
        await notify_user(username, remediation_name, "error", str(e))
        raise
    finally:
        if uploaded_file:
            await manager.client.files.delete(uploaded_file.id)
        if db_conn:
            db_conn.close()
        await manager.client.close()
        await manager_verify.client.close()

def start_remediation_job(filepath: str, username: str, remediation_name: str):
    """
    Synchronous wrapper that blocks until the async generation is complete.
    """
    return asyncio.run(background_process_document(filepath, username, remediation_name))