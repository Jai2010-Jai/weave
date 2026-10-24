# Weave — Conversational AI Software Development Platform

Weave is a conversational AI-powered software builder that creates real, installable Android apps and websites through plain-language conversations on **WhatsApp**, **SMS**, and **Web Chat**.

```text
WhatsApp (Meta Cloud API / Twilio) ─┐                          ┌─ Android: Gemini ─► validator ─► Gradle ─► APK verify ─► /download/<token>
SMS / Web Chat / Voice Notes ──────┼─► conversation + router ─┤
                                    │   (one SQLite store)     └─ Website: Weave generator (Node bridge) ─► /preview/<token>/
                                    └──────────── progress and results through the same outbox ─── publish: Netlify (optional)
```

> **Note on internal naming:** Weave was originally developed under the internal code name **Genesis**. To preserve backward compatibility with existing databases, migrations, and CLI tools, the core Python package remains `genesis`, and environment variables use the `GENESIS_*` prefix.

---

## Key Features

- **Multi-Channel Conversational Interface**: Access Weave via Meta WhatsApp Cloud API, Twilio SMS/WhatsApp, or the built-in Progressive Web App (PWA) Web Chat.
- **Natural Language Generation (NLG)**: Friendly, context-aware conversational replies powered by Google Gemini, paired with strict factual verification so build statuses, links, and tokens are never hallucinated.
- **Voice Note Understanding**: Send voice notes over WhatsApp or MMS, transcribed locally on-device using `faster-whisper`.
- **Android App Factory**: Turn prompts into native Kotlin + Jetpack Compose apps. Builds are compiled locally via Gradle, verified with Android build tools (`aapt2`, `apksigner`), and delivered as debug `.apk` downloads. Includes automatic error repair loops (up to 2 iterations).
- **Website Generator**: Build tailored multi-page websites and interactive previews served with sandboxed Content Security Policy (`CSP`).
- **Secure by Design**: Minimal process environments, strict path traversal checks, rate limiting, and zero secret leakage into child processes.

---

## Supported Platforms & Prerequisites

Weave runs on **Windows (native)**, **macOS**, and **Linux**.

### System Prerequisites

| Component | Minimum Version | Recommended | Notes |
|---|---|---|---|
| **Python** | 3.10+ | 3.12+ / 3.13 | Core runtime and API server |
| **Google Gemini API Key** | — | Required for AI generation | Free tier from [Google AI Studio](https://aistudio.google.com/apikey) |
| **Java Development Kit (JDK)** | JDK 17+ | JDK 17 / 21 | Eclipse Temurin, Android Studio JBR, or OpenJDK |
| **Android SDK** | API 35 | `platforms;android-35`, `build-tools;35.0.0` | Required only for Android `.apk` builds |
| **Node.js** | 20+ | LTS | Required only for the website builder engine |
| **Cloudflared** | Latest | Optional | For public HTTPS tunneling (WhatsApp webhooks & remote chat) |

---

## Quickstart & Installation

### 1. Clone & Set Up Python Environment

```bash
# Clone the repository
git clone https://github.com/hrizzReal/weave.git
cd weave

# Create a virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt
```

### 2. Configuration (`.env`)

Copy the template configuration file:

```bash
cp .env.example .env
```

Edit `.env` with your preferred settings:

```dotenv
# Required: Google Gemini AI
GEMINI_API_KEY=your-gemini-api-key-here
GEMINI_MODEL=auto

# Server & Dashboard
GENESIS_HOST=127.0.0.1
GENESIS_PORT=8765

# Conversational Brain & NLG
GENESIS_CHAT_BRAIN=on
GENESIS_CHAT_MODEL=gemini-3.5-flash-lite
GENESIS_NLG=on
GENESIS_NLG_MODEL=gemini-3.5-flash-lite
```

> **Security Notice**: `.env` is ignored by Git and must **never** be committed. Keep your keys strictly confidential.

### 3. Verify Prerequisites

Run the built-in diagnostic tool:

```bash
python -m genesis doctor
```

This verifies Python packages, JDK, Android SDK, Gradle wrapper, and Gemini API access without leaking any credentials.

---

## Running Weave

### Option A: Local Dashboard & Web Chat

Start the local server:

```bash
python -m genesis serve
```

- **Dashboard**: Visit [http://127.0.0.1:8765](http://127.0.0.1:8765) to monitor builds, test prompts, and manage project history.
- **Web Chat**: Visit [http://127.0.0.1:8765/chat](http://127.0.0.1:8765/chat) to interact with Weave directly in your browser.

### Option B: Public Web Chat & WhatsApp (with Cloudflare Tunnel)

To make Weave accessible from mobile phones and receive Meta WhatsApp webhooks:

**On Windows:**
```powershell
.\scripts\genesis_public.ps1
```

This starts:
1. The Weave FastAPI application on port 8765.
2. A Cloudflare Quick Tunnel forwarding traffic to your machine with an HTTPS public URL.
3. Automatically sets `PUBLIC_BASE_URL` so webhooks and token links work seamlessly.

---

## Hosting Websites on Your Domain (`<name>.weave.xyz`)

With `GENESIS_WEBSITE_DEPLOY=weave`, every finished website goes live on its own subdomain, served by this
Weave server. A change updates the same address.

```dotenv
GENESIS_WEBSITE_DEPLOY=weave
GENESIS_SITES_DOMAIN=weave.xyz
GENESIS_WEBSITE_PAGES=3   # extra pages (about, contact, ...) per new site, 0-4
```

Then send `*.weave.xyz` to this server. With a Cloudflare **named** tunnel (quick tunnels can't do wildcards):

```bash
cloudflared tunnel login
cloudflared tunnel create weave
cloudflared tunnel route dns weave '*.weave.xyz'
cloudflared tunnel route dns weave app.weave.xyz
cloudflared tunnel run --url http://127.0.0.1:8765 weave
```

Set `PUBLIC_BASE_URL=https://app.weave.xyz` so chat and WhatsApp links use the same tunnel. Names like
`www`, `app`, `api` and `chat` are reserved and never given to a customer site. Hosted sites are static,
read-only, served inside the same CSP sandbox as previews, and can't reach Weave's API. Files live in
`data/hosted/<name>/`.

## Configuring Meta WhatsApp Cloud API

To connect Weave to WhatsApp:

1. Create a Meta Developer App at [developers.facebook.com](https://developers.facebook.com/) and add the **WhatsApp** product.
2. In **WhatsApp > API Setup**, retrieve:
   - **Phone Number ID** (numeric ID)
   - **WhatsApp Business Account ID**
   - **Access Token** (System User token recommended for production)
3. In **App Settings > Basic**, retrieve your **App Secret**.
4. Configure these in your local `.env`:
   ```dotenv
   GENESIS_WHATSAPP_PROVIDER=meta
   META_ACCESS_TOKEN=your-meta-access-token
   META_PHONE_NUMBER_ID=your-meta-phone-number-id
   META_BUSINESS_ACCOUNT_ID=your-meta-business-account-id
   META_APP_SECRET=your-meta-app-secret
   META_VERIFY_TOKEN=your-random-verification-token
   ```
5. In Meta's WhatsApp **Configuration** tab, set:
   - **Callback URL**: `https://<your-public-tunnel-url>/api/webhooks/whatsapp`
   - **Verify Token**: Match `META_VERIFY_TOKEN` configured above.
   - **Webhook Fields**: Subscribe to `messages`.

> **Note regarding Meta Error 131037**: If you use a custom phone number, Meta requires you to submit the display name for approval in WhatsApp Manager before outbound messages can be sent. Meta pre-approved test numbers have no such restriction.

---

## Running the Automated Test Suite

Weave includes a comprehensive automated test suite covering all modules offline:

```bash
pytest -v
```

Tests verify:
- Webhook signature authentication (Meta HMAC-SHA256 and Twilio SHA1)
- Natural Language Generation (NLG) and factual integrity enforcement
- Gemini model discovery and graceful fallback
- Conversational chat brain routing and project selection
- Path traversal defense, file type validation, and forbidden API blocking
- APK verification (dex, manifest, zip structure, signatures)
- Sandboxed HTML website previews and security headers

---

## Security Architecture

Weave follows defensive security principles:

1. **No Secret Leakage**: API keys and tokens are server-side only. Child processes (such as Gradle builds) receive a scrubbed environment stripped of `GEMINI_API_KEY` and other sensitive variables.
2. **Path Traversal Defense**: All file creation in Android templates and website previews is strictly confined to designated workspace roots and cannot escape or traverse symlinks.
3. **Strict Content Security Policy (CSP)**: Website previews run in an isolated sandbox (`sandbox allow-scripts allow-popups`) with an opaque origin and strict CSP preventing script access to host cookies or APIs.
4. **Rate Limiting & Abuse Prevention**: Built-in sliding-window rate limiters protect against resource exhaustion and brute-force attempts on linking codes.
5. **No Dangerous Shell Subprocesses**: Subprocesses are invoked with explicit argument arrays, preventing command injection vulnerabilities.

---

## Important Disclaimers & Deployment Limitations

- **Publication on GitHub**: Pushing this codebase to GitHub does **not** automatically deploy Weave or host it 24/7. Weave is designed to run locally on developer hardware with GPU or CPU acceleration.
- **Debug Builds**: Generated Android APKs are signed with debug keys for testing. They are not intended for Google Play Store distribution without proper release key management.
- **Untrusted Code Execution**: While generated Kotlin source code undergoes strict AST and pattern validation, running builds on multi-tenant production systems should be paired with OS-level virtualization or containerization.

---

## License & Ownership

Copyright © 2026. All rights reserved.

This repository is published for source reference. All rights are reserved by the author. No license is granted to reproduce, distribute, or create derivative works without explicit written permission.
