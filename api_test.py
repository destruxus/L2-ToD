# api_test_compensated.py
import requests
import json
from datetime import datetime, timezone

# --- 1. FILL IN YOUR DETAILS AND THE *ACTUAL* TIME YOU WANT THE EVENT TO BE ---

# Get these from right-clicking in Discord
YOUR_SERVER_ID = "1002784757966442628"
YOUR_EVENTS_CHANNEL_ID = "1385273302943137932"
YOUR_DISCORD_USER_ID = "467599804692692992"

# Get this from running /api create in your server
YOUR_RAID_HELPER_API_KEY = "temBLJ9vtCSCc09ZJ9yDGGj4gNfgLBHwjbL9mpfJ"

# --- PROVIDE THE *ACTUAL* DESIRED START TIME ---
# The script will automatically subtract 4 hours to compensate for the API bug.
YOUR_DESIRED_START_TIMESTAMP = 1750431600 # We want the event at this time.


# --- 2. THE REST OF THE SCRIPT APPLIES THE FIX AND SENDS THE DATA ---

# Define some basic info for the payload
duration_hours = 4
boss_name = "API Test Boss (Compensated)"
boss_emoji = "🔧"

# --- THE FIX: Pre-compensate for the API's 4-hour error ---
COMPENSATION_IN_SECONDS = 14400 # 4 hours
timestamp_to_send = YOUR_DESIRED_START_TIMESTAMP - COMPENSATION_IN_SECONDS

# Convert the *compensated* timestamp into the required date and time strings in UTC
start_datetime_utc = datetime.fromtimestamp(timestamp_to_send, tz=timezone.utc)
date_str = start_datetime_utc.strftime('%Y-%m-%d')
time_str = start_datetime_utc.strftime('%H:%M')

# Construct the payload using the compensated date and time strings
payload = {
    "title": f"{boss_emoji} {boss_name} Window",
    "leaderId": YOUR_DISCORD_USER_ID,
    "leaderName": "APITester",
    "date": 1750431600,
    "time": 1750431600,
    "description": f"This is a direct API test with a 4-hour time compensation.\nThe goal is for this event to start at <t:{YOUR_DESIRED_START_TIMESTAMP}:F>.",
    "templateId": "standard",
    "advancedSettings": {
        "duration": duration_hours * 60,
        "utc": "true" 
    }
}

# Construct the URL and Headers
url = f"https://raid-helper.dev/api/v2/servers/{YOUR_SERVER_ID}/channels/{YOUR_EVENTS_CHANNEL_ID}/event"
headers = {
    "Authorization": YOUR_RAID_HELPER_API_KEY,
    "Content-Type": "application/json"
}

# --- 3. SEND THE REQUEST AND PRINT THE RESPONSE ---

print("--- Sending Request ---")
print(f"URL: {url}")
print("Payload Body (with compensated time):\n" + json.dumps(payload, indent=2))
print("-----------------------")

try:
    response = requests.post(url, json=payload, headers=headers, timeout=10)

    print("\n--- Response Received ---")
    print(f"Status Code: {response.status_code}")
    print("Response Body:")
    try:
        response_json = response.json()
        print(json.dumps(response_json, indent=2))

        received_time = response_json.get('event', {}).get('startTime')

        if received_time:
            print("\n--- Time Comparison ---")
            print(f"Our Goal Time (as Unix): {YOUR_DESIRED_START_TIMESTAMP}")
            print(f"Time Received (as Unix): {received_time}")
            if YOUR_DESIRED_START_TIMESTAMP == received_time:
                print("✅ SUCCESS! The times now match!")
            else:
                offset = received_time - YOUR_DESIRED_START_TIMESTAMP
                print(f"❌ The times still DO NOT match. Final Offset: {offset} seconds ({offset/3600:.2f} hours)")
            print("-----------------------")

    except json.JSONDecodeError:
        print(response.text)

except requests.exceptions.RequestException as e:
    print("\n--- An error occurred during the request ---")
    print(e)
