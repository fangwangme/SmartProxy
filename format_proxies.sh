#!/bin/bash

# ==============================================================================
# format_proxies.sh
#
# A shell script to format a proxy list by prepending a protocol
# and removing duplicate entries.
#
# Usage:
# ./format_proxies.sh <input_file> <protocol> <output_file>
#
# Example:
# ./format_proxies.sh raw_proxies.txt socks5 proxies.txt
# ==============================================================================

# --- 1. Validate Arguments ---
# Check if exactly three arguments are provided.
if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <input_file> <protocol> <output_file>"
    echo "Example: $0 raw_proxies.txt http proxies.txt"
    exit 1
fi

# Assign arguments to variables for clarity.
INPUT_FILE="$1"
PROTOCOL="$2"
OUTPUT_FILE="$3"

# Check if the input file exists and is readable.
if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: Input file not found at '$INPUT_FILE'"
    exit 1
fi

# --- 2. Process the File ---
# This command chain performs the main logic:
#
# 1. `grep -v '^\s*$' "$INPUT_FILE"`:
#    - Reads the input file.
#    - `-v` inverts the match, so it outputs lines that DO NOT match the pattern.
#    - `^\s*$` is a regular expression that matches empty lines or lines containing only whitespace.
#    - Result: A stream of non-empty lines from the input file.
#
# 2. `sed "s/^/$PROTOCOL:/"`:
#    - Takes the stream from grep.
#    - `s/old/new/` is the substitute command.
#    - `^` matches the beginning of each line.
#    - It replaces the beginning of each line with the specified protocol and a colon.
#    - Result: A stream of formatted proxy strings (e.g., "socks5:1.2.3.4:8080...").
#
# 3. `sort -u`:
#    - Takes the stream from sed.
#    - Sorts the lines alphabetically.
#    - `-u` (unique) flag ensures that only the first instance of any duplicate line is kept.
#    - Result: A sorted stream of unique, formatted proxy strings.
#
# 4. `> "$OUTPUT_FILE"`:
#    - Redirects the final stream from `sort -u` and writes it to the output file,
#      overwriting the file if it already exists.

grep -v '^\s*$' "$INPUT_FILE" | sed "s/^/$PROTOCOL:/" | sort -u > "$OUTPUT_FILE"


# --- 3. Final Output ---
# Count the number of unique proxies generated.
COUNT=$(wc -l < "$OUTPUT_FILE")

echo "Processing complete."
echo "Found and formatted $COUNT unique proxies."
echo "Output saved to: $OUTPUT_FILE"

