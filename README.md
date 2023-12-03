# Plist_Time_Dump - PList Timestamp Extractor
This project aims to bring unique visibility to mobile forensics investigations by enhancing the ability to seekout timestamps from various plists and export information about them with some contextual information, for review.

** Note: This is merely a side project for learning some coding skills (with an AI assist) and something that seemed like an interesting personal challenge. Please don't rely on this tool for expert information.

This Python script allows you to extract timestamps from PList files and convert them to ISO 8601 format. 

## Features

- Extracts timestamps from PList files in a specified directory.
- Converts timestamps to ISO 8601 format.
- Outputs the results to a tab-separated values (TSV) file.

## Requirements

- Python 3.x
- PList files (commonly used on Apple platforms)

## Usage

1. Clone this repository or download the script file.
2. Open your terminal or command prompt.
3. Run the script with the following command:

> python ChronoParser.py [directory_to_search] [output_file_path]

- `[directory_to_search]`: The directory path to search for PList files.
- `[output_file_path]`: The path for the output TSV file.

## Example Usage

`python ChronoParser.py /path/to/plist/files output.tsv`

Output

The script generates a TSV file containing the following columns:

- `UTC Timestamp` ISO 8601 formatted timestamp (UTC Assumed, output will need to be verified).
- `Original Value` Original timestamp value.
- `Timestamp Format` Detected timestamp format.
- `Key` The key in the PList file where the timestamp was found.
- `File Name` Name of the PList file.
- `Full Path` Full path to the PList file.

Sample Output
| UTC Timestamp           | Original Value        | Timestamp Format   | Key            | File Name          | Full Path                  |
|-------------------------|-----------------------|--------------------|----------------|--------------------|----------------------------|
| 2022-10-15T08:30:00.000000Z | 2022-10-15 08:30:00  | ISO 8601           | created_date   | data.plist         | /path/to/data.plist        |
| 2022-09-20T16:45:00.000000Z | 2022-09-20 16:45:00  | ISO 8601           | time_modified  | example.plist      | /path/to/example.plist     |
| 2022-11-05T12:15:00.000000Z | 2022-11-05 12:15:00  | ISO 8601           | event_time     | events.plist       | /path/to/events.plist      |
| 2021-12-03T14:30:00.000000Z | 1638533400           | UNIX Timestamp     | timestamp      | records.plist      | /path/to/records.plist     |

Author

Kevin Stokes

Script Version: 1.0

Feel free to use and modify this script. If you encounter any issues or have suggestions for improvements, please open an issue or pull request.

Happy timestamp extraction!
