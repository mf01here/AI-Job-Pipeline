import os
import json
import uuid
import shutil
import pymupdf as fitz
from openai import OpenAI
from dotenv import load_dotenv
from pydantic import ValidationError

from schemas.models import MasterFactBank, FactBankItem

load_dotenv()

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
client = OpenAI(base_url=LM_STUDIO_BASE_URL, api_key="lm-studio")

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extracts raw text from a PDF file."""
    text = ""
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text += page.get_text("text") + "\n"
    return text

def parse_text_to_facts(raw_text: str) -> MasterFactBank:
    """Uses the local LLM to extract structured facts from text."""
    schema_json = MasterFactBank.model_json_schema()
    
    prompt = f"""
    Extract the candidate's career history, skills, and verified accomplishments from the text below.
    Output strictly in JSON format matching this schema:
    {json.dumps(schema_json, indent=2)}
    
    Raw Text:
    {raw_text}
    """
    
    response = client.chat.completions.create(
        model="local-model",
        messages=[
            {"role": "system", "content": "You output valid JSON only. Do not include markdown formatting or explanations."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.1
        # Removed the response_format parameter causing the 400 error
    )
    
    raw_output = response.choices[0].message.content.strip()
    
    # Failsafe: Strip markdown backticks if the model wraps the JSON
    if "```json" in raw_output:
        raw_output = raw_output.split("```json")[1].split("```")[0]
    elif "```" in raw_output:
        raw_output = raw_output.split("```")[1].split("```")[0]
        
    parsed_dict = json.loads(raw_output.strip())
    return MasterFactBank(**parsed_dict)

def merge_fact_banks(existing_db: MasterFactBank, new_data: MasterFactBank) -> MasterFactBank:
    """Merges new data into the existing database while preventing duplicates."""
    # Merge and deduplicate skills/systems
    existing_db.languages_and_frameworks = list(set(existing_db.languages_and_frameworks + new_data.languages_and_frameworks))
    existing_db.infrastructure_and_systems = list(set(existing_db.infrastructure_and_systems + new_data.infrastructure_and_systems))
    existing_db.methodologies = list(set(existing_db.methodologies + new_data.methodologies))
    
    # Append new bullets and generate unique IDs to prevent collisions
    for bullet in new_data.verified_bullets:
        bullet.id = f"exp_{str(uuid.uuid4())[:8]}"
        existing_db.verified_bullets.append(bullet)
        
    # Update candidate name if it was previously unknown
    if not existing_db.candidate_name or existing_db.candidate_name == "Unknown":
        existing_db.candidate_name = new_data.candidate_name
        
    return existing_db

if __name__ == "__main__":
    # Setup directories
    os.makedirs("data/processed", exist_ok=True)
    db_path = "data/master_fact_bank.json"
    
    # 1. Load the existing database or initialize a new one
    if os.path.exists(db_path):
        with open(db_path, "r", encoding="utf-8") as f:
            master_db = MasterFactBank(**json.load(f))
        print(f"Loaded existing database with {len(master_db.verified_bullets)} bullets.")
    else:
        master_db = MasterFactBank(
            candidate_name="Unknown",
            languages_and_frameworks=[],
            infrastructure_and_systems=[],
            methodologies=[],
            verified_bullets=[]
        )
        print("Initializing new master database.")

    # 2. Find all unprocessed PDFs
    pdf_files = [f for f in os.listdir("data") if f.lower().endswith('.pdf') and not f.startswith('.')]
    
    if not pdf_files:
        print("No new PDF files found in 'data/'.")
    else:
        for pdf in pdf_files:
            file_path = os.path.join("data", pdf)
            print(f"\nProcessing {pdf}...")
            
            try:
                # Extract and Parse
                text = extract_text_from_pdf(file_path)
                if not text.strip():
                    print(f"Warning: {pdf} contains no extractable text.")
                    continue
                    
                new_facts = parse_text_to_facts(text)
                
                # Merge into Master Database
                master_db = merge_fact_banks(master_db, new_facts)
                
                # Archive the PDF so it isn't processed again next time
                shutil.move(file_path, os.path.join("data/processed", pdf))
                print(f"Success: Merged facts from {pdf} and archived file.")
                
            except Exception as e:
                print(f"Failed to process {pdf}: {e}")
                
        # 3. Save the updated database
        with open(db_path, "w", encoding="utf-8") as f:
            f.write(master_db.model_dump_json(indent=4))
            
        print(f"\nDatabase updated. Total verified bullets: {len(master_db.verified_bullets)}")