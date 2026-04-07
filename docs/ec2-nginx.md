# EC2 + Nginx Hosting Guide (Beginner Step-by-Step)

Goal: run this repo's WebSocket AudioHook server on an EC2 Ubuntu instance and expose it publicly as:

- `wss://daythree-ai.duckdns.org/audiohook`

Nginx terminates TLS (HTTPS/WSS) on `443` and reverse-proxies to the Python server on `127.0.0.1:8080`.

## 0) Before you start (what you must have)

Have these ready before deploying:

- Your domain: `daythree-ai.duckdns.org`
- Your repo URL (Git clone URL)
- Your Genesys Org ID (GUID): for `GENESYS_ORG_ID` (Genesys team can provide it)
- Your STT provider keys (ElevenLabs/Google/OpenAI) for transcription

Security Group inbound ports you will need:

- `80` from `0.0.0.0/0` (Let's Encrypt validation)
- `443` from `0.0.0.0/0` (Genesys will connect here)
- `22` from `My IP` (only needed if you use SSH; can be skipped if you use Session Manager)

If your DuckDNS token was ever shown in a screenshot or shared, rotate it in DuckDNS before going live.

## 1) Create / confirm your EC2 instance

1. AWS Console -> `EC2` -> `Instances` -> `Launch instances`
2. Name: `genesys-audiohook`
3. AMI: Ubuntu Server **22.04 LTS** or **24.04 LTS** (either works)
4. Instance type: `t3.small` (good start)
5. Key pair: create one and download the `.pem` (keep it safe)
6. Network settings / Security group inbound rules:
   - SSH 22 from `My IP`
   - HTTP 80 from `Anywhere-IPv4`
   - HTTPS 443 from `Anywhere-IPv4`
7. Launch.

Recommended: allocate and associate an **Elastic IP** so the IP does not change.

## 2) Make DuckDNS point to your EC2 IP

You have two options:

- Option A (recommended): Use an **Elastic IP** and update DuckDNS **once**.
- Option B: No Elastic IP -> set up a DuckDNS updater cron job on the instance.

### 2.1) Quick check (from your laptop)

Run:

```bash
nslookup daythree-ai.duckdns.org
```

Make sure it returns your EC2 public IP / Elastic IP.

### 2.2) (Optional) DuckDNS updater cron (only if your IP changes)

On the EC2 instance you will create:

- `/opt/duckdns/token` (your DuckDNS token)
- a cron job that calls DuckDNS every 5 minutes

You'll do this in Step 6 after the repo is cloned (because the script lives in `deploy/duckdns/duckdns.sh`).

## 3) Connect to the instance (AWS Console)

Recommended for beginners: use AWS Console in-browser SSH.

1. AWS Console -> `EC2` -> `Instances` -> select your instance.
2. Click `Connect`.
3. Choose `EC2 Instance Connect`.
4. Username: `ubuntu`
5. Click `Connect`.

If you are connected, you will see a prompt like:

```text
ubuntu@ip-...:~$
```

If EC2 Instance Connect fails:

- Confirm the instance has a public IPv4 and Status checks are passing
- Confirm the Security Group allows inbound `22` from your current public IP (`My IP`)
- If your org blocks SSH, use Session Manager instead (requires extra AWS setup)

## 4) Install packages (run on EC2)

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx git openssl
```

Sanity checks:

```bash
python3 --version
nginx -v
git --version
```

## 5) Put the code on the EC2 instance (clone the repo)

### 5.1) Create the app folder

```bash
sudo mkdir -p /opt/genesys-audiohook
sudo chown -R $USER:$USER /opt/genesys-audiohook
cd /opt/genesys-audiohook
```

### 5.2) Clone the repo (public repo)

```bash
git clone <your-repo-url> .
```

### 5.3) Clone the repo (private repo)

Use the HTTPS clone URL. When prompted:

- Username: your git username (or your email, depending on provider)
- Password: a **Personal Access Token** (PAT), not your normal password

```bash
git clone <your-repo-url> .
```

If you get stuck here, paste the exact error message and the git host (GitHub/GitLab/Bitbucket) and we'll pick the right auth method.

### 5.4) Confirm the repo is present

```bash
ls -la
ls -la deploy/nginx
python3 -c "import sys; print('OK', sys.version)"
```

You should see files like `main.py`, `config.py`, and folders like `deploy/`.

## 6) Create the Python venv + install dependencies

```bash
cd /opt/genesys-audiohook
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
deactivate
```

## 7) Create secrets + the `.env` file

### 7.1) Generate the values you'll paste into Genesys + your server

Run on EC2:

```bash
# API key (URL-safe hex). Put this in:
# - server .env as GENESYS_API_KEY
# - Genesys integration "API Key"
echo "GENESYS_API_KEY=$(openssl rand -hex 32)"

# Client secret (base64). Put this in Genesys integration "Client Secret".
# This repo does not validate it today, but Genesys requires the field.
echo "CLIENT_SECRET_BASE64=$(openssl rand -base64 32)"
```

Copy those two printed values somewhere safe (password manager). Do not commit them.

### 7.2) Create `/opt/genesys-audiohook/.env`

Create:

```bash
cat > /opt/genesys-audiohook/.env <<'EOF'
DEBUG=true

# Bind locally; Nginx exposes 443 publicly
GENESYS_LISTEN_HOST=127.0.0.1
GENESYS_LISTEN_PORT=8080
GENESYS_PATH=/audiohook

# REQUIRED (must match what Genesys sends)
GENESYS_API_KEY=REPLACE_ME
GENESYS_ORG_ID=REPLACE_ME

# STT provider (ElevenLabs)
DEFAULT_SPEECH_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=REPLACE_ME
# Optional override; default points to ElevenLabs realtime STT endpoint.
# ELEVENLABS_SCRIBE_WS_URL=wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime&commit_strategy=vad&audio_format=pcm_16000
ELEVENLABS_SCRIBE_STREAM_MODE=json_base64
# Resample Genesys 8kHz audio to 16kHz for ElevenLabs (recommended)
ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE=16000

# Optional (leave unset until needed)
# DAISY_BASE_URL=https://your-daisy-host
# DAISY_API_KEY=REPLACE_ME
EOF
```

Edit it:

```bash
nano /opt/genesys-audiohook/.env
```

Beginner tip for `nano`:

- Save: press `Ctrl+O`, then `Enter`
- Exit: press `Ctrl+X`

Notes:

- Keep `GENESYS_PATH=/audiohook` unless you have a reason to change it.
- `GENESYS_ORG_ID` is your Genesys Cloud Organization ID (GUID). If you don't have it, ask the Genesys team to provide it.

## 8) Start the app as a service (systemd)

```bash
sudo cp /opt/genesys-audiohook/deploy/systemd/genesys-audiohook.service /etc/systemd/system/genesys-audiohook.service
sudo systemctl daemon-reload
sudo systemctl enable genesys-audiohook
sudo systemctl start genesys-audiohook
sudo systemctl status genesys-audiohook --no-pager
```

Follow logs:

```bash
sudo journalctl -u genesys-audiohook -f
```

Health check directly against the Python server:

```bash
curl -i http://127.0.0.1:8080/health
```

You should see `OK`.

If the service fails:

```bash
sudo journalctl -u genesys-audiohook --no-pager -n 200
```

## 9) Configure DuckDNS updater cron (optional)

Only do this if you did NOT attach an Elastic IP.

```bash
sudo mkdir -p /opt/duckdns
sudo chown -R $USER:$USER /opt/duckdns

cat > /opt/duckdns/token <<'EOF'
REPLACE_WITH_YOUR_DUCKDNS_TOKEN
EOF
chmod 600 /opt/duckdns/token

cp /opt/genesys-audiohook/deploy/duckdns/duckdns.sh /opt/duckdns/duckdns.sh
nano /opt/duckdns/duckdns.sh
chmod 700 /opt/duckdns/duckdns.sh
```

Run once:

```bash
/opt/duckdns/duckdns.sh
tail -n 20 /opt/duckdns/duckdns.log
```

Schedule every 5 minutes:

```bash
crontab -e
```

Add:

```text
*/5 * * * * /opt/duckdns/duckdns.sh >/dev/null 2>&1
```

## 10) Configure Nginx reverse proxy (WebSocket)

```bash
sudo cp /opt/genesys-audiohook/deploy/nginx/audiohook.conf /etc/nginx/sites-available/audiohook.conf
sudo ln -sf /etc/nginx/sites-available/audiohook.conf /etc/nginx/sites-enabled/audiohook.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nano /etc/nginx/sites-available/audiohook.conf
```

Change:

- `server_name audiohook.yourcompany.com;` -> `server_name daythree-ai.duckdns.org;`

Test + reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Quick check through Nginx (HTTP):

```bash
curl -i http://localhost/health
```

If you get `502 Bad Gateway` here, update `/etc/nginx/sites-available/audiohook.conf` to add `proxy_http_version 1.1;` inside the `location = /health` block, then reload Nginx.

## 11) TLS certificate (Let's Encrypt)

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d daythree-ai.duckdns.org
sudo certbot renew --dry-run
```

If `certbot` fails, the two most common causes are:

- DNS does not point to this EC2 public IP yet
- Security Group inbound `80` is not open to the internet

## 12) Validate from outside (your laptop)

- `https://daythree-ai.duckdns.org/health` should return `OK`
- Genesys should connect to `wss://daythree-ai.duckdns.org/audiohook`

If `https://daythree-ai.duckdns.org/health` does not work:

1. Confirm DuckDNS points to the EC2 public IP (laptop): `nslookup daythree-ai.duckdns.org`
2. Confirm Nginx is running (EC2): `sudo systemctl status nginx --no-pager`
3. Confirm your app is running (EC2): `sudo systemctl status genesys-audiohook --no-pager`
4. Confirm inbound `443` is allowed in the Security Group

## 13) What to give the Genesys team (copy/paste)

- Connection URI (Audio WebSocket): `wss://daythree-ai.duckdns.org/audiohook`
- Properties:
  - Channel: `both`
  - Reconnections: `False (default)`
- Credentials (in Genesys UI):
  - API Key: `<GENESYS_API_KEY>`
  - Client Secret: `<CLIENT_SECRET_BASE64>`
- Headers (Genesys sends these automatically; you don't manually set them):
  - `X-API-KEY: <GENESYS_API_KEY>`
  - `Audiohook-Organization-Id: <GENESYS_ORG_ID>`
  - `Audiohook-Correlation-Id: <generated-by-genesys>`
  - `Audiohook-Session-Id: <generated-by-genesys>`
- Audio:
  - Format: `PCMU`
  - Rate: `8000 Hz`

### 13.1) What you need from the Genesys side

- Your Genesys Cloud **Organization ID** (GUID) for `GENESYS_ORG_ID`
- (Optional) Genesys egress IP ranges if you want to restrict inbound `443` to Genesys only

### 2-channel note

For 2-channel audio, this server maps:

- channel `0` -> `customer`
- channel `1` -> `agent`

If your environment is reversed, ask the Genesys team to include `customConfig.channelSpeakers` in the open message:

```json
{ "channelSpeakers": ["agent", "customer"] }
```

## 14) How to confirm audio is arriving (once Genesys tests)

App logs:

```bash
sudo journalctl -u genesys-audiohook -f
```

Look for:

- `Session opened. Negotiated media format`
- `Received audio frame from Genesys: ... bytes`
- `Processing transcription response on channel ...`

Nginx logs:

```bash
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

## 15) (Temporary) Debug UI during testing

This repo can expose a simple debugging page that streams events and transcripts during a test call.

1) On EC2, set a token in `/opt/genesys-audiohook/.env`:

```text
DEBUG_UI_TOKEN=REPLACE_ME_WITH_A_LONG_RANDOM_TOKEN
```

Optional (saves per-call audio to WAV files under `DEBUG_AUDIO_DIR`):

```text
DEBUG_SAVE_AUDIO=true
DEBUG_AUDIO_DIR=debug_audio
```

2) Restart the service:

```bash
sudo systemctl restart genesys-audiohook
```

3) Ensure Nginx has `/debug` and `/debug/ws` locations (the provided `deploy/nginx/audiohook.conf` includes them). Reload Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

4) Open in your browser:

- `https://daythree-ai.duckdns.org/debug?token=<DEBUG_UI_TOKEN>`

Disable the debug UI when done by removing `DEBUG_UI_TOKEN` (and restarting) to avoid exposing transcripts publicly.
