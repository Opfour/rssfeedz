#!/usr/bin/env python3
"""
OPML to feedlist.txt Converter
Converts OPML files to simple text format for rssfeedz

Usage:
    python3 opml_to_feedlist.py input.opml [output.txt]
    python3 opml_to_feedlist.py *.opml combined_feeds.txt
"""

import xml.etree.ElementTree as ET
import sys
import os
from pathlib import Path


def extract_feeds_from_opml(opml_file):
    """Extract all RSS feed URLs from an OPML file."""
    feeds = []
    
    try:
        tree = ET.parse(opml_file)
        root = tree.getroot()
        
        # Find all outline elements with xmlUrl attribute
        for outline in root.iter('outline'):
            xml_url = outline.get('xmlUrl')
            if xml_url:
                # Get the feed title if available
                title = outline.get('title') or outline.get('text') or ''
                feeds.append({
                    'url': xml_url,
                    'title': title
                })
        
        return feeds
    except Exception as e:
        print(f"Error parsing {opml_file}: {e}")
        return []


def process_opml_files(input_files, output_file='feedlist.txt', add_comments=True):
    """Process one or more OPML files and output to feedlist.txt format."""
    all_feeds = []
    seen_urls = set()  # To avoid duplicates
    
    # Handle glob patterns and multiple files
    files_to_process = []
    for pattern in input_files:
        if '*' in pattern or '?' in pattern:
            # Glob pattern
            files_to_process.extend(Path('.').glob(pattern))
        else:
            files_to_process.append(Path(pattern))
    
    # Process each OPML file
    for opml_file in files_to_process:
        if not opml_file.exists():
            print(f"Warning: {opml_file} not found, skipping...")
            continue
            
        print(f"Processing {opml_file}...")
        feeds = extract_feeds_from_opml(opml_file)
        
        # Add header comment for this file
        if add_comments and feeds:
            all_feeds.append({
                'url': f"# ========================================",
                'title': ''
            })
            all_feeds.append({
                'url': f"# Feeds from: {opml_file.name}",
                'title': ''
            })
            all_feeds.append({
                'url': f"# ========================================",
                'title': ''
            })
        
        # Add feeds, avoiding duplicates
        for feed in feeds:
            if feed['url'] not in seen_urls:
                seen_urls.add(feed['url'])
                all_feeds.append(feed)
        
        print(f"  Found {len(feeds)} feeds")
    
    # Write output
    with open(output_file, 'w', encoding='utf-8') as f:
        for feed in all_feeds:
            if add_comments and feed['title'] and not feed['url'].startswith('#'):
                # Add title as comment
                f.write(f"# {feed['title']}\n")
            f.write(f"{feed['url']}\n")
            if add_comments and not feed['url'].startswith('#'):
                f.write('\n')  # Add blank line for readability
    
    print(f"\n✓ Converted {len(seen_urls)} unique feeds to {output_file}")
    return len(seen_urls)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nExamples:")
        print("  python3 opml_to_feedlist.py feeds.opml")
        print("  python3 opml_to_feedlist.py tech_feeds.opml my_feeds.txt")
        print("  python3 opml_to_feedlist.py *.opml combined.txt")
        print("  python3 opml_to_feedlist.py tech.opml news.opml linux.opml all_feeds.txt")
        sys.exit(1)
    
    # Parse arguments
    input_files = sys.argv[1:-1] if len(sys.argv) > 2 and not sys.argv[-1].endswith('.opml') else sys.argv[1:]
    output_file = sys.argv[-1] if len(sys.argv) > 2 and not sys.argv[-1].endswith('.opml') else 'feedlist.txt'
    
    # Check if we should add comments
    add_comments = '--no-comments' not in sys.argv
    if '--no-comments' in sys.argv:
        input_files = [f for f in input_files if f != '--no-comments']
    
    # Process files
    try:
        count = process_opml_files(input_files, output_file, add_comments)
        print(f"\n✓ Done! Ready to use with rssfeedz.")
        print(f"  Copy {output_file} to ~/rssfeedz/feedlist.txt")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
