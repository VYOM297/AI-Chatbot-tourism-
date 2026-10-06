#!/bin/bash
# 1) action server in the background (internal, port 5055 - NOT exposed)
python -m rasa_sdk --actions actions --port 5055 &
# 2) Rasa server in the foreground on the port Spaces exposes
exec rasa run --enable-api --cors "*" --port 7860 --endpoints endpoints.yml
