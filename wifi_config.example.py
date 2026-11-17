# Wi-Fi settings for the clock.
# Copy this file to wifi_config.py, fill in your network, and copy it to the board.
# wifi_config.py is git-ignored so the password is not committed.
# The ESP32-S3 only supports 2.4 GHz networks.

SSID = "YOUR_WIFI_NAME"
PASSWORD = "YOUR_WIFI_PASSWORD"

# Standard-time offset from UTC in minutes (Pacific = -480, India = 330).
UTC_OFFSET_MINUTES = -480

# "US" applies US daylight saving rules; None for a fixed offset all year.
DST = "US"

NTP_HOST = "pool.ntp.org"

# Private iCal address of your calendar (Google Calendar: Settings > your calendar >
# "Secret address in iCal format"). Treat it like a password. Leave empty to disable.
CALENDAR_URL = ""
