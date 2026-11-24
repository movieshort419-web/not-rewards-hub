# Railway deployment — phone-friendly

## 1. Create a Railway project
Open Railway and create a new project/service.

## 2. Upload this folder
If Railway's dashboard offers a direct source upload/deploy option, upload this project folder. If your current Railway flow requires GitHub, create a private GitHub repository and upload these files, then connect that repository.

Railway detects the root `Dockerfile` automatically.

## 3. Add Variables
In the service Variables tab add:

BOT_TOKEN=YOUR_NEW_BOTFATHER_TOKEN
ADMIN_IDS=YOUR_TELEGRAM_NUMERIC_ID
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_SECRET_KEY=YOUR_SUPABASE_SECRET_KEY
AD_SMARTLINK_URL=YOUR_MONETAG_SMARTLINK
AD_POSTBACK_SECRET=MAKE_A_LONG_RANDOM_SECRET

Keep the reward variables at the defaults in `.env.example` unless you intentionally change them.

## 4. Deploy
Railway builds the Dockerfile and starts `python bot.py`.

## 5. Health check
After deployment, the service exposes:
GET /health

## 6. Telegram test
Open your bot and send:
/start

Then test:
- Mining -> start session
- Balance
- Invite
- Sponsored

For an admin test ad reward:
/verifyad USER_ID test-event-1

Do not enable real payout until the database, ad verification, withdrawal flow and NOT payout wallet have been separately tested.
