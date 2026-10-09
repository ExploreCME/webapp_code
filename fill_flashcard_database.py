import os
import json
import pymysql
import pymysql.cursors
import asyncio
from dotenv import load_dotenv

# Import the shared AI wrapper and utilities to keep code DRY
from Question_writing_and_verification_optomized import (
    ConversationManager, 
    clean_json_response, 
    standardize_categories
)

# 1. Point exactly to where your .env file lives
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# 2. Securely fetch the DB credentials from the .env file
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

# Standard NCCPA Task Areas (Canonical Standard)
PANCE_TASK_AREAS = [
    "History Taking and Performing Physical Examination",
    "Using Laboratory and Diagnostic Studies",
    "Formulating Most Likely Diagnosis",
    "Managing Patients - Health Maintenance, Patient Education, and Preventive Measures",
    "Managing Patients - Clinical Intervention",
    "Managing Patients - Pharmaceutical Therapeutics",
    "Applying Foundational Scientific Concepts"
]

# ==========================================
# FLASHCARD PROMPTS CONSTANTS (4-BULLET MAX)
# ==========================================
FLASHCARD_SYSTEM = "You are an experienced clinical professor in a Physician Assistant program."
FLASHCARD_TASK1 = "Create a high-yield short answer flashcard (question and answer) for Physician Assistant students preparing for board exams (PANCE/PANRE)."
FLASHCARD_TASK2 = "Target Organ System: "
FLASHCARD_TASK3 = "Topic Area: "
FLASHCARD_TASK4 = "Task Area: "

FLASHCARD_CONSTRAINTS = """Follow these strict rules:
- **Single-Part Questions Only**: Write a single, focused question for each card. Do NOT ask multi-part, compound, or double-barreled questions (e.g., do NOT ask "What features distinguish X from Y, AND what complications occur?"). Stick strictly to ONE target inquiry per flashcard.
- **No Patient Vignettes**: Do NOT use clinical vignettes or specific patient scenarios (e.g., do not start with "A 45-year-old male presents..."). Ask direct, straightforward, fact-based questions.
- **Prompt Design**: Write a targeted short answer question testing the specific Task Area for the given Topic. **CRITICAL: Do NOT explicitly state the name of the Task Area in your question.** (Example: For the task area 'Clinical Intervention', ask "What is the initial medication of choice for an acute asthma exacerbation?")
- **Answer Design**: Provide a direct, concise short answer. Use Markdown bullet points (use "-") to clearly state the core answer and related high-yield key pearls. **CRITICAL: You MUST use a MAXIMUM of 4 bullet point items in total per flashcard.** Emphasize board-tested facts a PA student must know for the PANCE/PANRE.
- **Alignment**: Ensure the generated question and answer strictly correspond to the indicated organ system, topic area, and task area.
"""

FLASHCARD_OUTPUT = """Output your response as valid JSON with this exact structure:
{
  "organ_system": "str",
  "topic": "str",
  "task_area": "str",
  "prompt": "str",
  "answer": "str"
}
Respond with JSON only. No explanations or markdown.
"""

Flashcard_Verification_SYSTEM = "You are an experienced clinical professor in a Physician Assistant program acting as an educational quality reviewer."
Flashcard_Verification_TASK = "Your task is to critically evaluate both the generated short answer question (prompt) and its corresponding high-yield answer for clinical accuracy, medical validity, and relevance to Physician Assistant board examinations."
Flashcard_Verification_CONSTRAINTS = """Follow these strict evaluation rules:
- Verify that the prompt is a single-part question and NOT a multi-part or compound question containing multiple sub-questions.
- Verify that the medical information, facts, diagnoses, and treatments stated in the answer are entirely clinically accurate and directly answer the question.
- Check that the question and answer strictly adhere to the requested Task Area for the specific Topic and provide a high-yield summary appropriate for board review.
- Verify that the answer uses no more than 4 bullet points.
- Provide a confidence score from 1 to 10 where 10 means absolute clinical accuracy and high yield.
"""
Flashcard_Verification_OUTPUT = """Output your response as valid JSON with this exact structure:
{
  "is_accurate": true,
  "confidence_score": 10
}
Respond with JSON only. No explanations or markdown.
"""

# Helper prompt builders
def build_Flashcard_prompt(organ_system, topic_area, task_area):
    return f"{FLASHCARD_SYSTEM}\n{FLASHCARD_TASK1}\n{FLASHCARD_TASK2}{organ_system}\n{FLASHCARD_TASK3}{topic_area}\n{FLASHCARD_TASK4}{task_area}\n{FLASHCARD_CONSTRAINTS}\n{FLASHCARD_OUTPUT}".strip()

def build_Flashcard_Verification_prompt(f_data):
    return f"{Flashcard_Verification_SYSTEM}\n{Flashcard_Verification_TASK}\nPROMPT:\n{f_data.get('prompt')}\n\nANSWER:\n{f_data.get('answer')}\n\n{Flashcard_Verification_CONSTRAINTS}\n{Flashcard_Verification_OUTPUT}".strip()

# ==========================================
# ASYNC GENERATION LOGIC
# ==========================================

async def Create_and_Validate_flashcard(conv_manager, organ_system, topic_area, task_area):
    prompt = build_Flashcard_prompt(organ_system, topic_area, task_area)
    response = await conv_manager.chat_completion(prompt, max_tokens=1500)
    try:
        return json.loads(clean_json_response(response))
    except json.JSONDecodeError:
        print(f"DEBUG: Failed to decode JSON for flashcard creation.")
        return None

async def Flashcard_Verification(conv_manager_verify, f_data):
    verification_count = 0
    passed_count = 0
    while verification_count < 3:
        prompt = build_Flashcard_Verification_prompt(f_data)
        response = await conv_manager_verify.chat_completion(prompt, max_tokens=1000)
        try:
            extracted_data = json.loads(clean_json_response(response))
            is_accurate = extracted_data.get("is_accurate", False)
            
            raw_confidence = extracted_data.get("confidence_score", 0)
            try:
                confidence = int(raw_confidence)
            except (ValueError, TypeError):
                confidence = 0
                
            if is_accurate and confidence >= 8:
                passed_count += 1
        except json.JSONDecodeError:
            pass
        verification_count += 1

    if passed_count == 3:
        return True, f_data
    return False, None

async def process_extracted_item(item_dict, conv_manager, conv_manager_verify):
    organ_system = item_dict.get("organ_system")
    task_area = item_dict.get("task_area")
    topic_area = item_dict.get("topic_area")
    if not (organ_system and task_area and topic_area):
        return None

    is_good_flashcard = False
    attempts = 0
    MAX_ATTEMPTS = 5
    while not is_good_flashcard and attempts < MAX_ATTEMPTS:
        f_data = await Create_and_Validate_flashcard(conv_manager, organ_system, topic_area, task_area)
        if f_data:
            is_good_flashcard, final_f_data = await Flashcard_Verification(conv_manager_verify, f_data)
            if is_good_flashcard:
                return final_f_data
        attempts += 1
    return None

async def heartbeat(interval=10):
    """Background task to print periodic messages while script is processing."""
    elapsed = 0
    try:
        while True:
            await asyncio.sleep(interval)
            elapsed += interval
            print(f"⏳ Script still running... ({elapsed}s elapsed)")
    except asyncio.CancelledError:
        pass

async def generate_flashcards_by_organ_system(json_path):
    try:
        with open(json_path, 'r') as f:
            topics_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find '{json_path}'.")
        return

    print("\n--- Available Organ Systems for Flashcards ---")
    systems_list = list(topics_data.keys())
    for idx, sys in enumerate(systems_list, 1):
        print(f"{idx}. {sys}")

    choice_str = input(f"\nEnter the NUMBER of the Organ System you want to process (1-{len(systems_list)}): ").strip()
    try:
        choice_idx = int(choice_str)
        if choice_idx < 1 or choice_idx > len(systems_list):
            raise ValueError
    except ValueError:
        print(f"Error: '{choice_str}' is not a valid selection.")
        return

    choice = systems_list[choice_idx - 1]
    selected_topics = topics_data[choice]
    tasks_to_process = []

    for topic in selected_topics:
        for task_area in PANCE_TASK_AREAS:
            tasks_to_process.append({
                "organ_system": choice,
                "topic_area": topic,
                "task_area": task_area
            })

    # ==========================================
    # INTERACTIVE TESTING MODE
    # ==========================================
    test_mode_str = input("\nEnable testing mode? (Y/N, limits generation to 10 flashcards): ").strip().upper()
    if test_mode_str == 'Y':
        tasks_to_process = tasks_to_process[:10]
        print(f"\n[TEST MODE ENABLED] Trimmed tasks down to {len(tasks_to_process)} flashcards for quick testing.")
    else:
        print(f"\n[FULL MODE] Proceeding with all {len(tasks_to_process)} flashcards.")

    total_cards_requested = len(tasks_to_process)

    conv_manager = ConversationManager(model="gpt-5.6-sol", temperature=1.0)
    conv_manager_verify = ConversationManager(model="gpt-5.6-sol", temperature=1.0)

    successful_flashcards = []
    semaphore = asyncio.Semaphore(10)

    async def safe_process(item):
        async with semaphore:
            return await process_extracted_item(item, conv_manager, conv_manager_verify)

    generation_tasks = [safe_process(item) for item in tasks_to_process]

    print(f"\nProcessing {total_cards_requested} flashcards asynchronously (10 at a time)...")
    heartbeat_task = asyncio.create_task(heartbeat(interval=10))

    try:
        for future in asyncio.as_completed(generation_tasks):
            try:
                result = await future
                if result:
                    clean_organ, clean_task = standardize_categories(
                        result.get("organ_system"),
                        result.get("task_area")
                    )
                    successful_flashcards.append((
                        clean_organ,
                        result.get("topic"),
                        clean_task,
                        result.get("prompt"),
                        result.get("answer")
                    ))
            except Exception as e:
                print(f"DEBUG: Task error: {e}")
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

    # ==========================================
    # MYSQL BATCH INSERTION
    # ==========================================
    if successful_flashcards:
        print(f"\nWriting {len(successful_flashcards)} verified flashcards to MySQL...")
        conn = None
        try:
            conn = pymysql.connect(
                host=MYSQL_HOST,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                database=MYSQL_DB,
                cursorclass=pymysql.cursors.DictCursor
            )
            cursor = conn.cursor()

            # Flashcards has a simple, flat structure (id INT AUTO_INCREMENT PRIMARY KEY)
            cursor.executemany("""
                  INSERT INTO flashcards (
                      organ_system, topic, task_area, prompt, answer
                  ) VALUES (%s, %s, %s, %s, %s)
            """, successful_flashcards)
            
            conn.commit()
            print(f"DEBUG: Successfully committed {len(successful_flashcards)} verified flashcards to database.")
        except Exception as e:
            print(f"DEBUG: MySQL Error: {e}")
        finally:
            if conn:
                conn.close()
    else:
        print("DEBUG: No flashcards passed the verification process.")

    print("All flashcard generation tasks completed.")

if __name__ == "__main__":
    JSON_SOURCE = "topics_by_organ_system.json"
    asyncio.run(generate_flashcards_by_organ_system(JSON_SOURCE))