import csv
import re
import os

def clean_phone_number(raw_phone: str) -> str:
    if not raw_phone:
        return ""
    
    # Take only the first number if separated by comma or space
    first_part = re.split(r'[, ]+', raw_phone.strip())[0]
    
    # Remove non-numeric characters
    cleaned = re.sub(r'\D', '', first_part)
    
    # Format to Indonesian code (62) if it starts with 0
    if cleaned.startswith('0'):
        cleaned = '62' + cleaned[1:]
        
    return cleaned

def parse_contacts_csv_content(csv_text: str):
    import io
    valid_contacts = []
    f = io.StringIO(csv_text.strip())
    reader = csv.DictReader(f)
    for row in reader:
        # Case insensitive key lookup
        row_lower = {k.strip().lower(): v for k, v in row.items() if k}
        raw_phone = row_lower.get("phone") or row_lower.get("phone_number") or row_lower.get("telepon") or ""
        cleaned_phone = clean_phone_number(raw_phone)
        if cleaned_phone:
            valid_contacts.append({
                "name": (row_lower.get("name") or row_lower.get("nama") or "").strip(),
                "phone": cleaned_phone,
                "label": (row_lower.get("label") or row_lower.get("tags") or "").strip() or None
            })
    return valid_contacts

def parse_umat_csv(filepath: str = "umat.csv"):
    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found.")
        return []
        
    with open(filepath, mode='r', encoding='utf-8') as file:
        return parse_contacts_csv_content(file.read())

