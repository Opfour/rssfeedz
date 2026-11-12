#!/bin/bash
# RSS Feed Update Script
# Automatically regenerates the rssfeedz HTML page

cd /home/david/rssfeedz
/usr/bin/python3 parsefeeds.py >> /home/david/rssfeedz/update.log 2>&1
echo "Updated at $(date)" >> /home/david/rssfeedz/update.log
