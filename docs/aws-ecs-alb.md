# AWS Hosting Guide (ECS Fargate + ALB) for Genesys AudioHook (WebSocket)

This repo runs a **WebSocket server** (`main.py`) that Genesys AudioHook connects to at:

- `wss://<your-domain>/audiohook`

Recommended AWS architecture:

```
Genesys Cloud (AudioHook WS)
  -> ALB (TLS 443, WebSocket)
    -> ECS Fargate task (HTTP ws:// :8080)
      -> Python websockets server
```

## 1) Prereqs (you must decide)

- A DNS name you control, e.g. `audiohook.yourcompany.com`
- TLS certificate in **ACM** for that hostname (in the same region as your ALB)
- Values you will set as environment variables in ECS:
  - `GENESYS_API_KEY` (required)
  - `GENESYS_ORG_ID` (required)
  - STT provider config (e.g. `DEFAULT_SPEECH_PROVIDER=elevenlabs` + ElevenLabs vars)

## 2) Containerize and push to ECR

From your machine (where you have AWS CLI configured):

```bash
aws ecr create-repository --repository-name genesys-audiohook
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <account_id>.dkr.ecr.<region>.amazonaws.com

docker build -t genesys-audiohook:latest .
docker tag genesys-audiohook:latest <account_id>.dkr.ecr.<region>.amazonaws.com/genesys-audiohook:latest
docker push <account_id>.dkr.ecr.<region>.amazonaws.com/genesys-audiohook:latest
```

## 3) Create an ALB (Application Load Balancer)

### Listener + cert

- Listener: **443 (HTTPS)**
- Attach ACM cert for `audiohook.yourcompany.com`

### Target group

- Target type: **IP**
- Protocol: **HTTP**
- Port: **8080**
- Health check:
  - Path: `/health`
  - Success codes: `200`

### ALB idle timeout (important for WebSockets)

Set ALB idle timeout to something higher than your expected call duration (e.g. 3600s).

## 4) ECS Cluster + Task Definition (Fargate)

### Task definition (minimum settings)

- Launch type: **Fargate**
- Task CPU/Memory: start with `0.5 vCPU / 1GB` (adjust later)
- Container port mapping: `8080/tcp`
- Logging driver: **awslogs** (CloudWatch)

### Environment variables (set in the container)

Required:

- `GENESYS_LISTEN_HOST=0.0.0.0`
- `GENESYS_LISTEN_PORT=8080`
- `GENESYS_PATH=/audiohook`
- `GENESYS_API_KEY=...`
- `GENESYS_ORG_ID=...`

STT (pick one):

- **ElevenLabs (recommended for your current setup)**
  - `DEFAULT_SPEECH_PROVIDER=elevenlabs`
  - `ELEVENLABS_API_KEY=...`
  - `ELEVENLABS_SCRIBE_WS_URL=wss://...`
  - optional `ELEVENLABS_SCRIBE_START_MESSAGE_JSON=...`
  - optional `ELEVENLABS_SCRIBE_STREAM_MODE=binary` (or `json_base64`)

- **Google**
  - `DEFAULT_SPEECH_PROVIDER=google`
  - `GOOGLE_CLOUD_PROJECT=...`
  - `GOOGLE_APPLICATION_CREDENTIALS=<service-account-json-string>`
  - `GOOGLE_SPEECH_MODEL=chirp_2`

- **OpenAI**
  - `DEFAULT_SPEECH_PROVIDER=openai`
  - `OPENAI_API_KEY=...`
  - `OPENAI_SPEECH_MODEL=...`

Agent assist to DAISY (optional; can be disabled initially):

- set `DAISY_BASE_URL` to enable, omit it to disable

### Secrets handling (recommended)

Put keys in **AWS Secrets Manager** and inject into task definition as secrets:

- `GENESYS_API_KEY`, `ELEVENLABS_API_KEY`, etc.

## 5) ECS Service behind ALB

- Create ECS Service (Fargate)
- Attach it to the ALB target group (port 8080)
- Desired tasks: start with 1 (increase later)

## 6) Security groups / networking

ALB Security Group:

- Inbound: `443` from the internet *or* (better) from Genesys egress IP ranges
- Outbound: allow to ECS tasks

ECS Task Security Group:

- Inbound: `8080` from ALB security group
- Outbound:
  - allow to ElevenLabs `wss://...` (and/or Google/OpenAI)
  - allow to DAISY if enabled

## 7) What to give the Genesys team

Give them:

- Endpoint: `wss://audiohook.yourcompany.com/audiohook`
- Required headers they must send:
  - `X-API-KEY: <GENESYS_API_KEY>`
  - `Audiohook-Organization-Id: <GENESYS_ORG_ID>`
- Audio format:
  - Must offer/stream `PCMU` at `8000 Hz`
- Optional: tell them you support 2-channel and you map:
  - channel 0 → customer
  - channel 1 → agent

## 8) How to view logs (CloudWatch)

If you configured awslogs on the container:

- ECS Console → Service → Tasks → select running task → “Logs”
- Or CloudWatch Logs → log group for your service

Useful strings to filter:

- `New session started`
- `Session opened. Negotiated media format`
- `Received audio frame from Genesys`
- `Processing transcription response`

If you see `opened` but never see `Received audio frame`, the WebSocket is up but Genesys isn’t streaming audio (media/connector config issue).

