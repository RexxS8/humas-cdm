import csv
import re
import os

def clean_phone_number(raw_phone: str) -> str:
    """
    Cleans raw phone number inputs into standard Indonesian format (e.g., 628569873731).
    Handles spaces, dashes, parentheses, + prefix, and leading zeroes.
    """
    if not raw_phone:
        return ""
    
    # Strip whitespace & quotes
    cleaned_str = str(raw_phone).strip().strip("'").strip('"')
    if not cleaned_str:
        return ""
    
    # If multiple numbers separated by comma, semicolon, or newline, take the first one
    first_part = re.split(r'[\r\n,;]+', cleaned_str)[0].strip()
    
    # Remove all non-digit characters
    digits = re.sub(r'\D', '', first_part)
    
    if not digits:
        return ""
    
    # Convert leading '0' to '62'
    if digits.startswith('0'):
        digits = '62' + digits[1:]
    # Convert numbers missing country code (e.g. 8569873731 -> 628569873731)
    elif not digits.startswith('62') and len(digits) >= 9 and len(digits) <= 13:
        digits = '62' + digits
        
    return digits


def parse_contacts_csv_content(csv_text: str):
    import io
    valid_contacts = []
    f = io.StringIO(csv_text.strip())
    reader = csv.DictReader(f)
    for row in reader:
        if not row:
            continue
        # Case insensitive key lookup
        row_lower = {k.strip().lower(): v for k, v in row.items() if k}
        raw_phone = row_lower.get("phone") or row_lower.get("phone_number") or row_lower.get("telepon") or row_lower.get("wa") or ""
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
