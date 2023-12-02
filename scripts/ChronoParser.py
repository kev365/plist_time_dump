import os
import plistlib
import csv
import re
from datetime import datetime, timezone

# Author
Kevin Stokes

# Define the script version number
SCRIPT_VERSION = "1.0"

# Constants for various timestamp conversions
EPOCH_1601 = datetime(1601, 1, 1)
MICROSECONDS_PER_SECOND = 1000000

def determine_timestamp_format(timestamp_str):
    # Check if it's in ISO 8601 format
    if re.match(r'^\d{4}-\d{2}-\d{2}$|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z', timestamp_str):
        return "ISO 8601"

    # Check if it's in Unix timestamp format (seconds)
    elif re.match(r'\d{10}|\d{13}|d{16}|\d{19}', timestamp_str):
        return "Unix Timestamp"

    # Add more checks for different timestamp formats here

    else:
        return "Unknown format"

def convert_unix_timestamp(timestamp_str):
    try:
        # Check if the timestamp is in seconds
        if '.' not in timestamp_str:
            timestamp_str = timestamp_str + '.000000'  # Assume seconds and add microseconds
        timestamp_float = float(timestamp_str)
        
        # Ensure it's a valid positive timestamp (adjust the range as needed)
        if 0 <= timestamp_float < 2**32:
            # Convert to UTC datetime
            timestamp_utc = datetime.fromtimestamp(timestamp_float, tz=timezone.utc)
            # Format as ISO 8601
            formatted_timestamp = timestamp_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            return formatted_timestamp
        else:
            return None
    except ValueError:
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

def process_directory(directory_path, output_file_path):
    with open(output_file_path, 'w', newline='', encoding='utf-8') as output_file:
        csv_writer = csv.writer(output_file, delimiter='\t')
        csv_writer.writerow(['UTC Timestamp', 'Original Value', 'Timestamp Format', 'Key', 'File Name', 'Full Path'])
        
        for root, _, files in os.walk(directory_path):
            for file in files:
                if file.endswith('.plist'):
                    plist_path = os.path.join(root, file)
                    try:
                        with open(plist_path, 'rb') as plist_file:
                            plist_data = plistlib.load(plist_file)
                    except (plistlib.InvalidFileException, ValueError):
                        # Handle exceptions when parsing the plist
                        continue

                    timestamps = extract_timestamps_from_plist(plist_data)
                    if timestamps:
                        for timestamp, original_value, key in timestamps:
                            full_path = os.path.abspath(plist_path)
                            file_name = os.path.basename(plist_path)
                            timestamp_format = determine_timestamp_format(original_value)
                            csv_writer.writerow([timestamp, original_value, timestamp_format, key, file_name, full_path])
                    
                    # Display the currently evaluated PList file in the terminal
                    print(f"Evaluating: {plist_path}")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=f"Extract timestamps from PList files and convert them to ISO 8601 format (Version {SCRIPT_VERSION})")
    parser.add_argument("directory_to_search", help="The directory path to search for PList files.")
    parser.add_argument("output_file_path", help="The path for the output TSV file.")
    args = parser.parse_args()
    
    process_directory(args.directory_to_search, args.output_file_path)
    print(f"Processing complete. Results exported to {args.output_file_path}")
