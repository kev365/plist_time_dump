import os
import plistlib
import csv
import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

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
    elif re.match(r'^\d+$', timestamp_str):
        timestamp_value = int(timestamp_str)
        if len(timestamp_str) == 10 or timestamp_value >= UNIX_HFS_THRESHOLD:
            return "Unix_timestamp"
        else:
            return "HFS_timestamp"

    else:
        return "Unknown_format"

def is_timestamp_in_range(timestamp_str, years=15):
    """Check if the timestamp is within 'years' years from the current year."""
    try:
        timestamp_year = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ").year
        current_year = datetime.now().year
        return current_year - years <= timestamp_year <= current_year + years
    except ValueError:
        # In case of parsing error, consider the timestamp as out of range
        return False

def convert_unix_timestamp(timestamp_str):
    try:
        if '.' not in timestamp_str:
            timestamp_str += '.000000'
        timestamp_float = float(timestamp_str)
        
        if timestamp_float < 0 or timestamp_float >= 2**32:
            raise ValueError("Timestamp out of valid range.")

        timestamp_utc = datetime.fromtimestamp(timestamp_float, tz=timezone.utc)
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

            # Check if the key contains "timestamp", "date", or "time" (case-insensitive)
            if re.search(r'date|time', key, re.IGNORECASE) and isinstance(value, str):
                formatted_timestamp = parse_utc_timestamp(value)
                if formatted_timestamp and "1970" not in formatted_timestamp:
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

def process_file(plist_path, output_file_path):
    with open(output_file_path, 'a', newline='', encoding='utf-8') as output_file:
        csv_writer = csv.writer(output_file, delimiter='\t')

        try:
            with open(plist_path, 'rb') as plist_file:
                plist_data = plistlib.load(plist_file)
        except (plistlib.InvalidFileException, ValueError):
            # Handle exceptions when parsing the plist
            return

        timestamps = extract_timestamps_from_plist(plist_data)
        if timestamps:
            for timestamp, original_value, key in timestamps:
                if is_timestamp_in_range(timestamp):  # Check if the timestamp is in range
                    full_path = os.path.abspath(plist_path)
                    file_name = os.path.basename(plist_path)
                    timestamp_format = determine_timestamp_format(original_value)
                    csv_writer.writerow([timestamp, original_value, timestamp_format, key, file_name, full_path])
        
        # Display the currently evaluated PList file in the terminal
        print(f"Evaluating: {plist_path}")

def process_directory(directory_path, output_file_path):
    # Prepare the output file with headers
    with open(output_file_path, 'w', newline='', encoding='utf-8') as output_file:
        csv_writer = csv.writer(output_file, delimiter='\t')
        csv_writer.writerow(['UTC Timestamp', 'Original Value', 'Timestamp Format', 'Key', 'File Name', 'Full Path'])

    # Use ThreadPoolExecutor to process files in parallel
    with ThreadPoolExecutor() as executor:
        futures = []
        for root, _, files in os.walk(directory_path):
            for file in files:
                if file.endswith('.plist'):
                    plist_path = os.path.join(root, file)
                    # Schedule the processing of each file
                    future = executor.submit(process_file, plist_path, output_file_path)
                    futures.append(future)

        # Wait for all threads to complete
        for future in futures:
            future.result()

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=f"Extract timestamps from PList files and convert them to ISO 8601 format (Version {SCRIPT_VERSION})")
    parser.add_argument("directory_to_search", help="The directory path to search for PList files.")
    parser.add_argument("output_file_path", help="The path for the output TSV file.")
    args = parser.parse_args()
    
    process_directory(args.directory_to_search, args.output_file_path)
    print(f"Processing complete. Results exported to {args.output_file_path}")
