import os
import plistlib
import csv
import re
from datetime import datetime, timezone, timedelta

# Define the script version number
SCRIPT_VERSION = "1.0"

# Constants for various timestamp conversions
EPOCH_1601 = datetime(1601, 1, 1)
EPOCH_1904 = datetime(1904, 1, 1)  # HFS+ epoch
MICROSECONDS_PER_SECOND = 1000000
UNIX_HFS_THRESHOLD = 1577836800  # 50 years after Unix epoch in seconds

def determine_timestamp_format(timestamp_str):
    # Check if it's in ISO 8601 format
    iso8601_regex = (r'^\d{4}-\d{2}-\d{2}$|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
                     r'|^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z'
                     r'|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z')
    if re.match(iso8601_regex, timestamp_str):
        return "ISO_8601"

    # Check for custom format YYYY-MM-DD_HHMMSS-TZ
    elif re.match(r'^\d{4}-\d{2}-\d{2}_\d{6}-\d{4}$', timestamp_str):
        return "Custom_format"

    # Check if it's a numeric timestamp (Unix or HFS+)
    elif timestamp_str.isdigit():
        timestamp_value = int(timestamp_str)
        if len(timestamp_str) == 10 or (len(timestamp_str) > 10 and timestamp_value < 2**32):
            return "Unix_timestamp"
        else:
            return "HFS_timestamp"

    else:
        return "Unknown_format"

def is_timestamp_in_range(timestamp_str, years=15, validate=False):
    """
    Check if the timestamp is within acceptable ranges and formats.
    Args:
        timestamp_str: The timestamp string to validate
        years: Number of years to allow before/after current year
        validate: If True, perform additional validation checks
    Returns:
        tuple: (is_valid, reason) if validate=True, otherwise just boolean
    """
    try:
        timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        current_year = datetime.now().year
        
        if not validate:
            # Original behavior - just check year range
            return current_year - years <= timestamp.year <= current_year + years
            
        # Additional validation checks
        issues = []
        
        # Check for future dates
        if timestamp > datetime.now(timezone.utc):
            issues.append("future_date")
            
        # Check for dates before 1970
        if timestamp.year < 1970:
            issues.append("pre_1970")
            
        # Check if too far from current date
        if timestamp.year < current_year - years:
            issues.append("too_old")
        elif timestamp.year > current_year + years:
            issues.append("too_future")
            
        if issues:
            return False, ",".join(issues)
        return True, "valid"
        
    except ValueError:
        if validate:
            return False, "invalid_format"
        return False

def convert_unix_timestamp(timestamp_str):
    try:
        # Convert the timestamp string to a float
        timestamp_float = float(timestamp_str)

        # Check if the timestamp is out of the valid range for Unix timestamps
        if timestamp_float < 0 or timestamp_float >= 2**32:
            raise ValueError("Timestamp out of valid range.")

        # Convert the Unix timestamp to a UTC datetime object
        # If the original timestamp included microseconds, they will be preserved
        timestamp_utc = datetime.fromtimestamp(timestamp_float, tz=timezone.utc)

        # Format the timestamp into ISO 8601 format
        formatted_timestamp = timestamp_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        return formatted_timestamp
        
    except ValueError as e:
        # Log the error or print a message
        print(f"Error converting Unix timestamp: {e}")
        return None
    except OverflowError:
        # Handle cases where the timestamp is too large
        print("Unix timestamp is too large.")
        return None
    except Exception as e:
        # Handle any other unforeseen exceptions
        print(f"Unexpected error occurred: {e}")
        return None

def convert_hfs_timestamp(timestamp_str):
    try:
        timestamp_int = int(timestamp_str)
        timestamp_utc = EPOCH_1904 + timedelta(seconds=timestamp_int)
        return timestamp_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as e:
        print(f"Error converting HFS+ timestamp: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error occurred: {e}")
        return None

def parse_utc_timestamp(timestamp_str):
    # Try to parse as ISO 8601 timestamp
    try:
        timestamp_utc = datetime.fromisoformat(timestamp_str)
        if timestamp_utc.tzinfo is None:
            timestamp_utc = timestamp_utc.replace(tzinfo=timezone.utc)
        return timestamp_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        pass

    # Try to parse as Unix timestamp
    formatted_timestamp = convert_unix_timestamp(timestamp_str)
    if formatted_timestamp:
        return formatted_timestamp

    # Handling HFS+ timestamps
    if determine_timestamp_format(timestamp_str) == "HFS_timestamp":
        return convert_hfs_timestamp(timestamp_str)

    # Handling custom format
    if determine_timestamp_format(timestamp_str) == "Custom Format":
        return convert_custom_format_to_utc(timestamp_str)

def convert_custom_format_to_utc(timestamp_str):
    try:
        # Split the datetime and timezone parts
        datetime_part, tz_offset = timestamp_str.split('-')
        datetime_formatted = datetime.strptime(datetime_part, '%Y-%m-%d_%H%M%S')

        # Process the timezone offset
        # The timezone is in the format of "-HHMM" or "+HHMM"
        tz_sign = -1 if tz_offset[0] == '-' else 1
        tz_hours = int(tz_offset[1:3])
        tz_minutes = int(tz_offset[3:5])
        total_offset = timedelta(hours=tz_hours, minutes=tz_minutes) * tz_sign

        # Adjust to UTC
        timestamp_utc = datetime_formatted - total_offset
        return timestamp_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    except ValueError as e:
        print(f"Error converting custom timestamp: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error occurred: {e}")
        return None

def extract_timestamps_from_plist(plist_data, current_key_path=""):
    timestamps = []
    found_timestamp = False

    if isinstance(plist_data, dict):
        for key, value in plist_data.items():
            current_key = f"{current_key_path}/{key}" if current_key_path else key


            # Existing check for keys containing "timestamp", "date", or "time"
            if re.search(r'date|time', key, re.IGNORECASE) and isinstance(value, str):
                formatted_timestamp = parse_utc_timestamp(value)
                if formatted_timestamp and "1970" not in formatted_timestamp:
                    timestamps.append((formatted_timestamp, value, current_key))
                found_timestamp = True

            # New check for numeric values
            elif isinstance(value, str) and value.isdigit():
                length = len(value)
                if length in [10, 13, 16, 19]:
                    timestamp_format = determine_timestamp_format(value)
                    if timestamp_format in ["Unix_timestamp", "HFS_timestamp"]:
                        formatted_timestamp = parse_utc_timestamp(value)
                        if formatted_timestamp:
                            timestamps.append((formatted_timestamp, value, current_key))
                            found_timestamp = True

            # Recursively search nested dictionaries
            if isinstance(value, (dict, list)):
                nested_timestamps = extract_timestamps_from_plist(value, current_key)
                if nested_timestamps:
                    timestamps.extend(nested_timestamps)
                    found_timestamp = True

    elif isinstance(plist_data, list):
        for item in plist_data:
            if isinstance(item, (dict, list)):
                nested_timestamps = extract_timestamps_from_plist(item, current_key_path)
                if nested_timestamps:
                    timestamps.extend(nested_timestamps)
                    found_timestamp = True

    return timestamps if found_timestamp else None

def get_file_type(plist_path):
    """Determine if the file is a plist or a bplist."""
    try:
        with open(plist_path, 'rb') as file:
            header = file.read(8)  # Read first 8 bytes for header
            if header.startswith(b'bplist'):
                return 'bplist'
            elif header.startswith(b'<?xml'):
                return 'plist'
            else:
                return 'unknown'
    except Exception as e:
        print(f"Error determining file type: {e}")
        return 'error'

def process_file(plist_path, output_file_path, validate=False):
    with open(output_file_path, 'a', newline='', encoding='utf-8') as output_file:
        csv_writer = csv.writer(output_file, delimiter='\t')

        file_type = get_file_type(plist_path)

        try:
            with open(plist_path, 'rb') as plist_file:
                plist_data = plistlib.load(plist_file)
        except (plistlib.InvalidFileException, ValueError):
            # Handle exceptions when parsing the plist
            return

        timestamps = extract_timestamps_from_plist(plist_data)
        if timestamps:
            for timestamp, original_value, key in timestamps:
                validation_result = is_timestamp_in_range(timestamp, validate=validate)
                
                if validate:
                    is_valid, reason = validation_result
                else:
                    is_valid = validation_result
                    reason = "not_validated"
                
                if not validate or (validate and is_valid):
                    full_path = os.path.abspath(plist_path)
                    file_name = os.path.basename(plist_path)
                    timestamp_format = determine_timestamp_format(original_value)
                    row = [timestamp, original_value, timestamp_format, key, 
                          file_type, file_name, full_path]
                    if validate:
                        row.append(reason)
                    csv_writer.writerow(row)
        
        print(f"Evaluating: {plist_path}")

def process_directory(directory_path, output_file_path, validate=False):
    # Prepare the output file with headers
    with open(output_file_path, 'w', newline='', encoding='utf-8') as output_file:
        csv_writer = csv.writer(output_file, delimiter='\t')
        headers = ['UTC Timestamp', 'Original Value', 'Timestamp Format', 
                  'Key', 'File Type', 'File Name', 'Full Path']
        if validate:
            headers.append('Validation')
        csv_writer.writerow(headers)

    for root, _, files in os.walk(directory_path):
        for file in files:
            if file.endswith('.plist') or file.endswith('.bplist'):
                plist_path = os.path.join(root, file)
                process_file(plist_path, output_file_path, validate)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=f"Extract timestamps from PList files and convert them to ISO 8601 format (Version {SCRIPT_VERSION})")
    parser.add_argument("directory_to_search", help="The directory path to search for PList files.")
    parser.add_argument("output_file_path", help="The path for the output TSV file.")
    parser.add_argument("--validate", action="store_true", 
                       help="Enable additional timestamp validation and mark suspicious entries")
    args = parser.parse_args()
    
    process_directory(args.directory_to_search, args.output_file_path, args.validate)
    print(f"Processing complete. Results exported to {args.output_file_path}")
