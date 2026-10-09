###EOR_Extraction_optimized.py
import os
import json
import difflib
from openai import OpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import List, Literal

# Point exactly to where your .env file lives
load_dotenv('/home/ps51632/mysite/explorecme/.env')
# Fetch the key
api_key = os.getenv('MY_API_KEY')


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
# ==========================================

def standardize_extracted_organ(raw_organ):
    """Normalizes raw input strings into strict PANCE categories or 'Misc'."""
    exact_organs = [
        "Cardiovascular System", "Dermatologic System", "Endocrine System",
        "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
        "Genitourinary System", "Hematologic System", "Infectious Diseases",
        "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
        "Pulmonary System", "Renal System", "Reproductive System", "Professional Practice"
    ]

    raw_organ_str = str(raw_organ).strip() if raw_organ else ""
    if ":" in raw_organ_str:
        prefix = raw_organ_str.split(":")[0].strip()
        organ_lower = prefix.lower()
    else:
        organ_lower = raw_organ_str.lower()

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

    organ_match = difflib.get_close_matches(clean_organ, exact_organs, n=1, cutoff=0.3)

    if organ_match:
        return organ_match[0]
    elif clean_organ in exact_organs:
        return clean_organ
    else:
        return "Misc"

class ConversationManager:
    def __init__(self, api_key=None, model=None, temperature=None, max_tokens=None):
        self.client = OpenAI(api_key=api_key or DEFAULT_API_KEY)
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
"""

def eor_extract_information(conv_manager, uploaded_file_id, output_file_path=None):
    prompt = conv_manager.build_eor_extraction_prompt()

    json_path = "/home/ps51632/mysite/explorecme/topics_by_organ_system.json"
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
            item["organ_system"] = standardize_extracted_organ(raw_organ)

            organ_system = item.get("organ_system")
            raw_topic = item.get("topic_area")

            valid_topics = canonical_topics_data.get(organ_system, [])

            if valid_topics and raw_topic:
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
            folder_path = '/home/ps51632'
            os.makedirs(folder_path, exist_ok=True)
            file_path = os.path.join(folder_path, 'eor_question_extraction.txt')
            with open(file_path, 'a') as f:
                f.write(json.dumps(creation_extracted_data) + '\n')
            print("Data successfully extracted, matched, and saved to default location.")

    except Exception as e:
        print(f"Failed to process extraction. Error was: {e}")

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
    eor_pdf = "/home/ps51632/EOR_report.pdf"
    sample_output_path = "/home/ps51632/my_custom_output.json"

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