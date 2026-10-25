###EOR_Extraction_optimized.py
import os
import json
import difflib
from openai import OpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import List, Literal
from standardizers import (
    DOCUMENT_CATEGORY_INSTRUCTION, SOURCE_ORGAN_ALIASES,
    source_organ_system, standardize_organ_system, standardize_task_area,
)

# Point exactly to where your .env file lives
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
# Fetch the key
DEFAULT_API_KEY = os.getenv('MY_API_KEY') or os.getenv('OPENAI_API_KEY')


DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_SYSTEM_MESSAGE = "You are a Professor in a Physician Assistant program."

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
        "Misc",
        *SOURCE_ORGAN_ALIASES
    ] = Field(description="Canonical organ system or explicitly listed source content category")

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
# ==========================================

def standardize_extracted_organ(raw_organ):
    """Normalizes raw input strings into strict PANCE categories or 'Misc'."""
    return standardize_organ_system(raw_organ)

class ConversationManager:
    def __init__(self, api_key=None, model=None, temperature=None, max_tokens=None):
        api_key = api_key or DEFAULT_API_KEY
        if not api_key:
            raise RuntimeError("Set MY_API_KEY or OPENAI_API_KEY in the environment or .env file.")
        self.client = OpenAI(api_key=api_key)
        self.temperature = temperature if temperature is not None else DEFAULT_TEMPERATURE
        self.model = model if model is not None else DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.system_message = DEFAULT_SYSTEM_MESSAGE

    def structured_completion(self, prompt, response_format, temperature=None, max_tokens=None, uploaded_file_id=None):
        messages = [
            {"role": "system", "content": self.system_message},
        ]

        user_content = [{"type": "text", "text": prompt}]
        if uploaded_file_id:
            user_content.append({"type": "file", "file": {"file_id": uploaded_file_id}})

        messages.append({"role": "user", "content": user_content})

        response = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=messages,
            temperature=temperature or self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            response_format=response_format
        )
        return response.choices[0].message.parsed

    def build_eor_extraction_prompt(self):
        return f"""{PANCE_Verification_SYSTEM}
{PANCE_Verification_TASK}
{PANCE_Verification_CONSTRAINTS}""".strip()

PANCE_Verification_SYSTEM = "You are an experience professor in a Physician Assistant program."
PANCE_Verification_TASK = "Your task is to evalaute the given file to identify the content catergory, task area, and keywords listed under the Keywords section."
PANCE_Verification_CONSTRAINTS = """Follow these rules:
- You must extract three distinct pieces of information for each entry:
  1. organ_system (Content Category)
  2. task_area (Task Area)
  3. topic_area (Keywords / Specific Medical Conditions)
- CRITICAL MAPPING: The "topic_area" MUST contain the specific diseases, medical conditions, or keywords listed in the document (e.g., "Acute bronchitis", "Hypertension"). Do NOT duplicate the task area into the topic area field.
- SOURCE CATEGORY RULE:
""" + DOCUMENT_CATEGORY_INSTRUCTION + """
- Otherwise map "organ_system" to one of the following categories:
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
"""

def eor_extract_information(conv_manager, uploaded_file_id, output_file_path=None):
    prompt = conv_manager.build_eor_extraction_prompt()

    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics_by_organ_system.json")
    try:
        with open(json_path, 'r') as f:
            canonical_topics_data = json.load(f)
    except FileNotFoundError:
        print(f"Warning: Could not find {json_path}. Proceeding without fuzzy matching.")
        canonical_topics_data = {}

    try:
        parsed_data = conv_manager.structured_completion(
            prompt=prompt,
            response_format=EORResponse,
            temperature=0.4,
            max_tokens=10000,
            uploaded_file_id=uploaded_file_id
        )

        creation_extracted_data = parsed_data.model_dump()

        for item in creation_extracted_data.get("extracted_topics", []):
            # Pre-normalize organ system using our matching logic to catch edge cases & prefixes
            raw_organ = item.get("organ_system")
            item["organ_system"] = source_organ_system(raw_organ)
            item["task_area"] = standardize_task_area(item.get("task_area"))

            organ_system = item.get("organ_system")
            raw_topic = item.get("topic_area")

            valid_topics = canonical_topics_data.get(organ_system, [])

            if organ_system not in SOURCE_ORGAN_ALIASES and valid_topics and raw_topic:
                matches = difflib.get_close_matches(raw_topic, valid_topics, n=1, cutoff=0.6)

                if matches:
                    print(f"Matched raw '{raw_topic}' -> Canonical '{matches[0]}'")
                    item["topic_area"] = matches[0]
                else:
                    print(f"No close match found for '{raw_topic}' in {organ_system}. Keeping raw string.")

        print(creation_extracted_data)

        if output_file_path:
            output_dir = os.path.dirname(output_file_path)
            if output_dir and not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)

            with open(output_file_path, 'a') as f:
                f.write(json.dumps(creation_extracted_data) + '\n')
            print(f"Data successfully extracted, matched, and saved to {output_file_path}.")
        else:
            raise ValueError("An explicit extraction output path is required.")

    except Exception as e:
        print(f"Failed to process extraction. Error was: {e}")
        raise

def extract_topics_from_openai_file(file_id, quiz_name, output_file_path):
    conv_manager = ConversationManager(api_key=os.getenv('OPENAI_API_KEY') or os.getenv('MY_API_KEY'))
    try:
        eor_extract_information(conv_manager, file_id, output_file_path=output_file_path)
    finally:
        conv_manager.client.close()


def extract_topics_from_eor_file(filepath, quiz_name, output_file_path):
    conv_manager = ConversationManager()
    uploaded_file = None
    try:
        with open(filepath, "rb") as file_data:
            uploaded_file = conv_manager.client.files.create(
                file=file_data,
                purpose="user_data"
            )
        print(f"File uploaded successfully to OpenAI. ID: {uploaded_file.id}")
        eor_extract_information(conv_manager, uploaded_file.id, output_file_path=output_file_path)

    finally:
        if uploaded_file:
            conv_manager.client.files.delete(uploaded_file.id)
            print(f"Cleaned up: Deleted file {uploaded_file.id} from OpenAI servers.")

if __name__ == "__main__":
    conv_manager = ConversationManager()
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("output")
    args = parser.parse_args()
    eor_pdf = args.pdf
    sample_output_path = args.output

    try:
        with open(eor_pdf, "rb") as file_data:
            uploaded_file = conv_manager.client.files.create(
                file=file_data,
                purpose="user_data"
            )
        print(f"File uploaded successfully. ID: {uploaded_file.id}")
        eor_extract_information(conv_manager, uploaded_file.id, output_file_path=sample_output_path)

    finally:
        if 'uploaded_file' in locals():
            conv_manager.client.files.delete(uploaded_file.id)
            print(f"Cleaned up: Deleted file {uploaded_file.id} from OpenAI servers.")