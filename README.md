# Eco-Travel Advisor — Rasa Chatbot

A conversational agent for sustainable tourism planning. Recommends eco-certified
accommodation, calculates approximate carbon footprints per transport mode, and
escalates complex cases to a human travel advisor with full conversation context.

## Project structure
```
eco-travel-advisor/
├── config.yml            # NLU pipeline + Core policies
├── domain.yml             # intents, entities, slots, responses, actions
├── endpoints.yml          # points Rasa at the action server
├── credentials.yml        # REST channel config
├── data/
│   ├── nlu.yml             # training examples per intent
│   ├── stories.yml         # example conversation paths
│   ├── rules.yml           # fallback + handover rules
│   └── eco_hotels.json     # mock eco-hotel database (API fallback)
├── actions/
│   └── actions.py          # custom actions: carbon calc, recommendations, handover
├── tests/
│   └── test_stories.yml    # stories used by `rasa test core`
├── Dockerfile              # Rasa server image
├── Dockerfile.actions      # action server image
├── docker-compose.yml
└── .env.example
```

## Option A — Run locally (no Docker)

1. Install Python 3.9 or 3.10 (Rasa does not yet support 3.11+).
2. Create a virtual environment and install Rasa:
   ```bash
   python -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\activate
   pip install rasa rasa-sdk
   ```
3. Copy `.env.example` to `.env` and fill in your free API keys
   (Climatiq: https://www.climatiq.io/ · Amadeus: https://developers.amadeus.com/).
   Then load them into your shell, e.g.:
   ```bash
   export $(cat .env | xargs)      # macOS/Linux
   ```
4. Train the model:
   ```bash
   rasa train
   ```
5. In one terminal, start the action server:
   ```bash
   rasa run actions
   ```
6. In a second terminal, talk to the bot:
   ```bash
   rasa shell
   ```
   Or run the API + a browser-based chat:
   ```bash
   rasa run --enable-api --cors "*"
   ```

## Option B — Run with Docker (recommended for deployment)

1. Copy `.env.example` to `.env` and fill in your API keys.
2. Build and start both services:
   ```bash
   docker compose up --build
   ```
3. The Rasa REST API is now available at `http://localhost:5005/webhooks/rest/webhook`.
   The action server is internal-only (not exposed to the host), reachable only
   by the `rasa` service — this follows the brief's secure-endpoint guidance.

## Testing

```bash
# NLU intent/entity accuracy (80/20 split, confusion matrix)
rasa test nlu --cross-validation

# Dialogue/story test — checks story transitions and fallback handling
rasa test core --stories tests/test_stories.yml
```
Results are written to the `results/` folder (confusion matrix, failed stories, etc.).

## Deployment to Hugging Face Spaces (free, zero-cost)

1. Create a new Space → SDK: **Docker**.
2. Push this repo to the Space's git remote (Spaces build directly from your Dockerfile).
3. Add `CLIMATIQ_API_KEY`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` as **Secrets**
   in the Space settings (never commit them).
4. Because Spaces only exposes one port, either deploy the Rasa server as the
   public Space and run the action server as a background process in the same
   container, or use Spaces' Docker Compose support if enabled for your plan.

## Notes on API fallbacks

- If `CLIMATIQ_API_KEY` is not set (or the API call fails/times out), carbon
  estimates fall back to a static DEFRA-style emissions-factor table so the bot
  never crashes on a missing key.
- Eco-certification data has no reliable free API, so hotel results are enriched
  from the curated `data/eco_hotels.json` file regardless of whether Amadeus
  sandbox data is available.
